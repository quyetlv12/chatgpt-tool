"""One-way local MySQL replica. SQLite is always the authoritative store.

The outbox contains keys/revisions only, written by SQLite triggers in the
same transaction as account changes. Network work never holds a SQLite lock.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pymysql

from .engine import DatabaseEngine


JOB_SCOPE = "job_type IN ('twofa_community', 'password_community')"
SETTING_SCOPE = "key LIKE 'twofa.%' OR key = 'password_change.target_password'"
# Deliberately exclude session data, local control tokens, logs and legacy tables.
ENTITIES = {
    "jobs": ("id", JOB_SCOPE,
             "id,email,password,pending_password,secret,status,job_type,account_check,created_at,started_at,finished_at"),
    "twofa_history": ("job_id", "1", "job_id,email,password,secret,changed_at"),
    "password_history": ("job_id", "1", "job_id,email,password,secret,changed_at"),
    "settings": ("key", SETTING_SCOPE, "key,value,updated_at"),
    "live_journals": ("id", "1", "id,name,created_at,account_count,entries_json"),
}

MYSQL_DDL = """
CREATE TABLE IF NOT EXISTS tool_twofa_records (
    source_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    entity VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    record_key VARCHAR(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    revision BIGINT UNSIGNED NOT NULL,
    payload JSON NULL,
    deleted BOOLEAN NOT NULL DEFAULT FALSE,
    synced_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (source_id, entity, record_key)
) ENGINE=InnoDB
"""
# Keep tombstones and monotonic revisions: a lost commit acknowledgement or an
# older concurrent worker must never resurrect/overwrite a newer remote row.
MYSQL_UPSERT = """
INSERT INTO tool_twofa_records
    (source_id, entity, record_key, revision, payload, deleted)
VALUES (%s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    payload = IF(VALUES(revision) >= revision, VALUES(payload), payload),
    deleted = IF(VALUES(revision) >= revision, VALUES(deleted), deleted),
    revision = GREATEST(revision, VALUES(revision))
"""


def load_config(path: Path) -> dict | None:
    if not path.exists():
        return None
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Invalid local database configuration")
    if config.get("enabled", True) is False:
        return None
    # This feature is local-only; a remote server must use an explicit tunnel.
    if config.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Database host must be loopback")
    port = config.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid database port")
    if not isinstance(config.get("database"), str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", config["database"]):
        raise ValueError("Invalid database name")
    if not isinstance(config.get("user"), str) or not config["user"]:
        raise ValueError("Invalid database user")
    if not isinstance(config.get("password"), str):
        raise ValueError("Invalid database credential")
    return {key: config[key] for key in ("host", "port", "database", "user", "password")}


def configure_mamp(runtime: Path) -> Path:
    """Explicit setup using installed MAMP config; never echo its credentials."""
    text = Path("/Applications/MAMP/bin/phpMyAdmin/config.inc.php").read_text(encoding="utf-8")

    def field(key: str) -> str:
        match = re.search(r"\$cfg\['Servers'\]\[\$i\]\['" + key + r"'\]\s*=\s*'([^'\\]*)'\s*;", text)
        if not match:
            raise ValueError("MAMP configuration requires manual setup")
        return match.group(1)

    config = {"enabled": True, "host": "127.0.0.1", "port": int(field("port")),
              "database": "shoptktoool", "user": field("user"), "password": field("password")}
    runtime.mkdir(parents=True, exist_ok=True)
    target = runtime / "mysql-sync.json"
    # Refuse to replace an existing connection silently.
    if target.exists():
        load_config(target)
        return target
    fd, temporary = tempfile.mkstemp(prefix=".mysql-sync-", dir=runtime)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)  # mkstemp creates a private 0600 file.
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def safe_account_state(raw: str | None) -> dict:
    try:
        state = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    keys = ("mode", "phase", "rotated_pending_verify", "mutation_started", "login_verified",
            "retry_count", "error_kind", "account_state", "plan", "plan_source", "billing_date",
            "usage_enabled", "payment_methods_enabled")
    result = {key: state[key] for key in keys if key in state and not isinstance(state[key], (dict, list))}
    usage = state.get("usage")
    if isinstance(usage, dict):
        fields = ("used_percent", "remaining_percent", "limit_window_seconds", "reset_after_seconds",
                  "reset_at", "allowed", "limit_reached", "plan_type")
        result["usage"] = {key: usage[key] for key in fields if key in usage and not isinstance(usage[key], (dict, list))}
        for key, allowed in (("credits", ("has_credits", "unlimited", "balance")),
                             ("reset_credits", ("available_count",))):
            if isinstance(usage.get(key), dict):
                result["usage"][key] = {name: usage[key][name] for name in allowed
                                         if name in usage[key] and not isinstance(usage[key][name], (dict, list))}
    if "payment_methods" in state:
        from session_phase import _sanitize_payment_methods
        methods = state["payment_methods"]
        result["payment_methods"] = _sanitize_payment_methods(methods) if isinstance(methods, list) else None
    return result


class MySQLSync:
    def __init__(self, engine: DatabaseEngine, config_path: Path, *, connect=None, interval: float = 5):
        self.engine = engine
        self.config_path = config_path
        self._connect = connect or pymysql.connect
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = {"state": "disabled", "last_error": None, "last_success_at": None}
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()

    def prepare(self) -> None:
        """Install transactional markers, then backfill existing rows once."""
        with self.engine.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO twofa_sync_meta VALUES ('source_id', ?)", (str(uuid.uuid4()),))
            for entity, (key, scope, _fields) in ENTITIES.items():
                for action, ref in (("INSERT", "NEW"), ("UPDATE", "OLD"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
                    # OLD and NEW on UPDATE also cover a row leaving the allowlist.
                    condition = re.sub(r"\b(job_type|key)\b", lambda m: f"{ref}.{m[1]}", scope)
                    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS twofa_sync_{entity}_{action.lower()}_{ref.lower()}
                        AFTER {action} ON {entity} WHEN {condition}
                        BEGIN
                            INSERT INTO twofa_sync_outbox (entity, record_key)
                            VALUES ('{entity}', {ref}.{key})
                            ON CONFLICT(entity, record_key) DO UPDATE SET revision=excluded.revision;
                        END""")
            initialized = conn.execute("SELECT value FROM twofa_sync_meta WHERE key='initialized'").fetchone()
            known = json.loads(initialized[0]) if initialized is not None else []
            # v16 stored '1'. Re-seeding that installation once is idempotent;
            # future entity additions backfill only the newly introduced tables.
            known = known if isinstance(known, list) else []
            missing = [entity for entity in ENTITIES if entity not in known]
            if missing:
                self._seed(conn, missing)
                conn.execute("INSERT OR REPLACE INTO twofa_sync_meta VALUES ('initialized', ?)", (json.dumps(list(ENTITIES)),))

    @staticmethod
    def _seed(conn: sqlite3.Connection, entities: list[str] | None = None) -> None:
        for entity in ENTITIES if entities is None else entities:
            key, scope, _fields = ENTITIES[entity]
            conn.execute(f"""INSERT INTO twofa_sync_outbox (entity, record_key)
                SELECT ?, {key} FROM {entity} WHERE {scope}
                ON CONFLICT(entity, record_key) DO UPDATE SET revision=excluded.revision""", (entity,))

    def _batch(self) -> tuple[str, list[tuple]]:
        # ponytail: one bounded batch under the existing writer lock; no network
        # inside. A dedicated read snapshot is only needed if lock time grows.
        with self.engine.transaction(immediate=False) as conn:
            source = conn.execute("SELECT value FROM twofa_sync_meta WHERE key='source_id'").fetchone()[0]
            pending = conn.execute("SELECT * FROM twofa_sync_outbox ORDER BY revision LIMIT 100").fetchall()
            batch = []
            for item in pending:
                entity = item["entity"]
                key, scope, fields = ENTITIES[entity]
                row = conn.execute(f"SELECT {fields} FROM {entity} WHERE {key}=? AND ({scope})", (item["record_key"],)).fetchone()
                payload = dict(row) if row is not None else None
                if payload is not None and entity == "jobs":
                    payload["account_check"] = safe_account_state(payload.get("account_check"))
                batch.append((source, entity, item["record_key"], item["revision"],
                              json.dumps(payload, ensure_ascii=False, allow_nan=False) if payload is not None else None,
                              payload is None))
            return source, batch

    def sync_once(self) -> int:
        with self._sync_lock:
            return self._sync_once()

    def _sync_once(self) -> int:
        remote = None
        try:
            config = load_config(self.config_path)
            if config is None:
                self._set_status(state="disabled", last_error=None)
                return 0
            # Changing the target requeues current rows; it does not erase the
            # previous DB or another installation's source namespace.
            target = json.dumps([config["host"], config["port"], config["database"]])
            with self.engine.transaction() as conn:
                old = conn.execute("SELECT value FROM twofa_sync_meta WHERE key='target'").fetchone()
                if old is None or old[0] != target:
                    self._seed(conn)
                    conn.execute("INSERT OR REPLACE INTO twofa_sync_meta VALUES ('target', ?)", (target,))
            _source, batch = self._batch()
            remote = self._connect(**config, charset="utf8mb4", autocommit=False,
                                   connect_timeout=2, read_timeout=3, write_timeout=3)
            with remote.cursor() as cursor:
                cursor.execute(MYSQL_DDL)
                remote.begin()  # DDL is separate; it can implicitly commit.
                for record in batch:
                    if self._stop.is_set():
                        remote.rollback()
                        return 0
                    cursor.execute(MYSQL_UPSERT, record)
            remote.commit()
            # Ack only these exact revisions after remote commit. Writes that
            # raced with the network request retain their newer outbox marker.
            now = time.time()
            with self.engine.transaction() as conn:
                conn.executemany("DELETE FROM twofa_sync_outbox WHERE revision=?", [(row[3],) for row in batch])
                conn.execute("INSERT OR REPLACE INTO twofa_sync_meta VALUES ('last_success_at', ?)", (str(now),))
            self._set_status(state="online", last_error=None, last_success_at=now)
            return len(batch)
        except Exception as error:
            # Exception text can contain SQL values, credentials and host data.
            self._set_status(state="offline", last_error=type(error).__name__)
            return 0
        finally:
            if remote is not None:
                try:
                    remote.close()
                except Exception:
                    pass

    def _set_status(self, **values) -> None:
        with self._lock:
            self._status.update(values)

    def status(self) -> dict:
        pending = self.engine.raw_connection().execute("SELECT COUNT(*) FROM twofa_sync_outbox").fetchone()[0]
        with self._lock:
            return {**self._status, "pending": pending}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.prepare()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="twofa-mysql-sync", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            sent = self.sync_once()
            if self._stop.wait(0.05 if sent == 100 else self.interval):
                break

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description="Local 2FA MySQL synchronization")
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--configure-mamp", action="store_true")
    args = parser.parse_args()
    try:
        path = args.runtime / "twofa.db"
        if not path.is_file():
            raise ValueError("Existing tool database required")
        if args.configure_mamp:
            configure_mamp(args.runtime)
        # Back up before an existing runtime schema is migrated. No account
        # workers are constructed by this one-shot import command.
        if path.exists():
            backup = args.runtime / f"twofa-before-mysql-{time.time_ns()}.db"
            fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as source, closing(sqlite3.connect(backup)) as dest:
                source.backup(dest)
        engine = DatabaseEngine(path)
        try:
            sync = MySQLSync(engine, args.runtime / "mysql-sync.json")
            sync.prepare()
            while sync.sync_once() == 100:
                pass
            status = sync.status()
            print(json.dumps(status))
            return 0 if status["state"] == "online" and status["pending"] == 0 else 1
        finally:
            engine.close()
    except Exception as error:
        print(json.dumps({"state": "setup_failed", "error": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
