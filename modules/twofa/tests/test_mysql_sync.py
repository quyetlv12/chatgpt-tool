from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from db.engine import DatabaseEngine
from db.mysql_sync import MYSQL_DDL, MYSQL_UPSERT, MySQLSync, load_config, safe_account_state
from db.repositories import JobRepository, LiveJournalRepository, SettingsRepository
from db.schema import CURRENT_VERSION


class FakeMySQL:
    """Transactional sink that can lose the commit acknowledgement."""
    def __init__(self):
        self.rows = {}
        self.pending = {}
        self.fail_write = False
        self.lose_ack = False
        self.before_commit = None
        self.calls = 0

    def connect(self, **_kwargs):
        self.calls += 1
        self.pending = {}
        if self.fail_write:
            raise OSError("SQL and password must not reach status")
        return self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def begin(self):
        self.pending = {}

    def execute(self, sql, params=None):
        if sql == MYSQL_DDL:
            return
        assert sql == MYSQL_UPSERT
        key = params[:3]
        current = self.pending.get(key) or self.rows.get(key)
        if current is None or params[3] >= current[3]:
            self.pending[key] = params

    def commit(self):
        if self.before_commit:
            callback, self.before_commit = self.before_commit, None
            callback()
        self.rows.update(self.pending)
        self.pending = {}
        if self.lose_ack:
            self.lose_ack = False
            raise OSError("Commit succeeded but reply lost")

    def rollback(self):
        self.pending = {}

    def close(self):
        self.pending = {}


class MySQLSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.engine = DatabaseEngine(self.path / "twofa.db")
        self.repo = JobRepository(self.engine)
        self.settings = SettingsRepository(self.engine)
        self.config = self.path / "mysql-sync.json"
        self.config.write_text(json.dumps({"host": "127.0.0.1", "port": 8889,
            "database": "shoptktoool", "user": "test", "password": "synthetic"}))
        self.remote = FakeMySQL()
        self.sync = MySQLSync(self.engine, self.config, connect=self.remote.connect, interval=0.01)

    def tearDown(self):
        self.sync.stop()
        self.engine.close()
        self.temp.cleanup()

    def job(self, key="one", job_type="twofa_community"):
        self.repo.create({"id": key, "email": "synthetic@example.com", "password": "synthetic-password",
            "secret": "SYNTHETIC", "combo": "[redacted]", "created_at": 1,
            "job_type": job_type, "status": "success",
            "account_check": json.dumps({"login_verified": True, "sessionToken": "DO_NOT_SYNC",
                "usage": {"used_percent": 12, "access_token": "DO_NOT_SYNC"},
                "payment_methods": [{"type": "card", "brand": "visa", "last4": "1234", "id": "DO_NOT_SYNC"}]})})

    def row(self, entity="jobs", key="one"):
        return next(value for index, value in self.remote.rows.items() if index[1:] == (entity, key))

    def test_backfill_allowlist_no_credentials_in_status(self):
        self.job()
        self.job("password", "password_community")
        self.job("legacy", "signup")
        self.settings.set("web.auth_token", "DO_NOT_SYNC" * 4)
        self.settings.set("twofa.input_draft", "synthetic@example.com|synthetic|SYNTHETIC")
        self.sync.prepare()
        self.assertEqual(self.sync.sync_once(), 3)
        self.assertEqual(len(self.remote.rows), 3)
        encoded = json.dumps(list(self.remote.rows.values()))
        self.assertNotIn("DO_NOT_SYNC", encoded)
        self.assertNotIn("synthetic", json.dumps(self.sync.status()))
        self.assertEqual(json.loads(self.row()[4])["password"], "synthetic-password")
        self.assertEqual(self.sync.status()["pending"], 0)

    def test_offline_restart_and_lost_commit_ack_are_idempotent(self):
        self.sync.prepare()
        self.job()
        self.remote.fail_write = True
        self.assertEqual(self.sync.sync_once(), 0)
        self.assertEqual(self.sync.status()["pending"], 1)
        self.assertEqual(self.sync.status()["last_error"], "OSError")
        self.engine.close()
        self.engine = DatabaseEngine(self.path / "twofa.db")
        self.repo = JobRepository(self.engine)
        self.sync = MySQLSync(self.engine, self.config, connect=self.remote.connect)
        self.sync.prepare()
        self.remote.fail_write = False
        self.remote.lose_ack = True
        self.sync.sync_once()
        self.assertEqual(self.sync.status()["pending"], 1)
        self.assertEqual(len(self.remote.rows), 1)
        self.sync.sync_once()
        self.assertEqual(len(self.remote.rows), 1)
        self.assertEqual(self.sync.status()["pending"], 0)

    def test_update_and_delete_during_upload_keep_new_marker(self):
        self.sync.prepare()
        self.job()
        self.remote.before_commit = lambda: self.repo.update_status("one", "success", secret="UPDATED")
        self.sync.sync_once()
        self.assertEqual(self.sync.status()["pending"], 1)
        self.sync.sync_once()
        self.assertEqual(json.loads(self.row()[4])["secret"], "UPDATED")
        self.repo.update_status("one", "success", secret="FINAL")
        self.remote.before_commit = lambda: self.repo.delete("one")
        self.sync.sync_once()
        self.assertEqual(self.sync.status()["pending"], 1)
        self.sync.sync_once()
        self.assertTrue(self.row()[5])
        self.assertIsNone(self.row()[4])

    def test_history_survives_clear_and_rollback_has_no_marker(self):
        self.sync.prepare()
        self.job()
        self.sync.sync_once()
        with self.assertRaises(RuntimeError):
            with self.engine.transaction():
                self.repo.update_status("one", "success", secret="ROLLBACK")
                raise RuntimeError("rollback")
        self.assertEqual(self.sync.status()["pending"], 0)
        self.repo.complete_twofa_success(job_id="one", email="synthetic@example.com",
            password="synthetic", secret="VERIFIED", account_check="{}", changed_at=2)
        self.repo.delete_all("twofa_community")
        self.sync.sync_once()
        self.assertTrue(self.row()[5])
        self.assertFalse(self.row("twofa_history")[5])
        self.assertEqual(json.loads(self.row("twofa_history")[4])["secret"], "VERIFIED")

    def test_history_insert_ignore_does_not_drop_a_pending_revision(self):
        self.sync.prepare()
        self.repo.record_twofa_history(job_id="one", email="synthetic@example.com", password="synthetic", secret="FIRST", changed_at=1)
        _source, old = self.sync._batch()
        with self.engine.transaction() as conn:
            conn.execute("DELETE FROM twofa_history WHERE job_id='one'")
        self.repo.record_twofa_history(job_id="one", email="synthetic@example.com", password="synthetic", secret="SECOND", changed_at=2)
        _source, new = self.sync._batch()
        self.assertGreater(new[0][3], old[0][3])
        self.assertEqual(json.loads(new[0][4])["secret"], "SECOND")

    def test_background_reconnects_without_blocking_account_writes(self):
        self.remote.fail_write = True
        self.sync.start()
        self.job()
        deadline = time.monotonic() + 2
        while self.remote.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.repo.update_status("one", "success", secret="OFFLINE-UPDATE")
        self.remote.fail_write = False
        while self.sync.status()["pending"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.sync.status()["pending"], 0)
        self.assertEqual(json.loads(self.row()[4])["secret"], "OFFLINE-UPDATE")

    def test_invalid_or_disabled_config_preserves_pending_rows(self):
        self.sync.prepare()
        self.job()
        self.config.unlink()
        self.sync.sync_once()
        self.assertEqual(self.sync.status()["state"], "disabled")
        self.assertEqual(self.sync.status()["pending"], 1)
        self.config.write_text('{"host":"remote.example.com"}')
        self.sync.sync_once()
        self.assertEqual(self.sync.status()["state"], "offline")
        self.assertEqual(self.remote.calls, 0)

    def test_unavailable_or_legacy_payment_data_never_blocks_replication(self):
        for value in (None, "legacy-invalid", {}, []):
            with self.subTest(value=value):
                state = safe_account_state(json.dumps({"payment_methods": value, "login_verified": True}))
                self.assertTrue(state["login_verified"])
                self.assertEqual(state["payment_methods"], [] if value == [] else None)

    def test_mysql8_rsa_auth_dependency_works_without_cached_auth(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        from pymysql._auth import sha2_rsa_encrypt

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        salt = b"01234567890123456789"
        cipher = sha2_rsa_encrypt(b"synthetic", salt, public)
        decrypted = key.decrypt(cipher, padding.OAEP(padding.MGF1(hashes.SHA1()), hashes.SHA1(), None))
        expected = bytes(value ^ salt[index % 20] for index, value in enumerate(b"synthetic\0"))
        self.assertEqual(decrypted, expected)

    def test_v15_migration_retains_data(self):
        self.job()
        with self.engine.transaction() as conn:
            conn.execute("DELETE FROM _schema_version WHERE version=?", (CURRENT_VERSION,))
            conn.execute("INSERT INTO _schema_version(version) VALUES(15)")
            conn.execute("DROP TABLE twofa_sync_meta")
            conn.execute("DROP TABLE twofa_sync_outbox")
        self.engine.close()
        self.engine = DatabaseEngine(self.path / "twofa.db")
        self.sync = MySQLSync(self.engine, self.config, connect=self.remote.connect)
        self.sync.prepare()
        self.assertEqual(self.sync.sync_once(), 1)
        self.assertEqual(json.loads(self.row()[4])["password"], "synthetic-password")

    @unittest.skipUnless(os.environ.get("TWOFA_MYSQL_TEST_CONFIG"), "Explicit local MySQL integration opt-in")
    def test_real_mysql_offline_then_online_commit_retry_update_delete(self):
        config_path = Path(os.environ["TWOFA_MYSQL_TEST_CONFIG"])
        config = load_config(config_path)
        self.sync = MySQLSync(self.engine, config_path)
        self.sync.prepare()
        source, _batch = self.sync._batch()
        import pymysql
        try:
            self.job()
            _source, original = self.sync._batch()
            self.sync._connect = lambda **_kw: (_ for _ in ()).throw(ConnectionRefusedError())
            self.sync.sync_once()
            self.assertEqual(self.sync.status()["pending"], 1)
            self.sync._connect = pymysql.connect
            self.assertEqual(self.sync.sync_once(), 1)
            # Simulate local acknowledgement loss by replaying the same marker.
            with self.engine.transaction() as conn:
                conn.execute("INSERT INTO twofa_sync_outbox(entity,record_key) VALUES('jobs','one')")
            self.sync.sync_once()
            self.repo.update_status("one", "success", secret="UPDATED")
            self.sync.sync_once()
            with pymysql.connect(**config) as remote, remote.cursor() as cursor:
                cursor.execute("SELECT payload FROM tool_twofa_records WHERE source_id=%s AND entity='jobs' AND record_key='one'", (source,))
                rows = cursor.fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(json.loads(rows[0][0])["secret"], "UPDATED")
            self.repo.delete("one")
            self.sync.sync_once()
            with pymysql.connect(**config) as remote, remote.cursor() as cursor:
                cursor.execute(MYSQL_UPSERT, original[0])
                remote.commit()
                cursor.execute("SELECT deleted,payload FROM tool_twofa_records WHERE source_id=%s", (source,))
                self.assertEqual(cursor.fetchall(), ((1, None),))
            entries = [{"email": "synthetic@example.test", "password": "SYNTHETIC-PASSWORD",
                "secret": "SYNTHETIC-SECRET", "snapshot": {"id": "one", "status": "success", "account_state": "live"}}]
            journal = LiveJournalRepository(self.engine).create("Synthetic journal", entries)
            self.assertEqual(self.sync.sync_once(), 1)
            with pymysql.connect(**config) as remote, remote.cursor() as cursor:
                cursor.execute("SELECT payload FROM tool_twofa_records WHERE source_id=%s AND entity='live_journals' AND record_key=%s",
                    (source, journal["id"]))
                payload = json.loads(cursor.fetchone()[0])
                self.assertEqual(payload["account_count"], 1)
                self.assertEqual(json.loads(payload["entries_json"]), entries)
            self.assertEqual(self.sync.status()["pending"], 0)
        finally:
            # Only the synthetic namespace created by this test is removed.
            with pymysql.connect(**config) as remote, remote.cursor() as cursor:
                cursor.execute("DELETE FROM tool_twofa_records WHERE source_id=%s", (source,))
                remote.commit()


if __name__ == "__main__":
    unittest.main()
