from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def account_key(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:24]


class BackupStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.data_dir.chmod(0o700)
        except OSError:
            pass
        self.path = data_dir / "chat-backup.db"
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                source_key TEXT NOT NULL,
                target_key TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                share_url TEXT NOT NULL DEFAULT '',
                target_url TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source_key, target_key, conversation_id)
            );
            """
        )
        self._db.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def is_complete(self, source: str, target: str, conversation_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT status FROM items WHERE source_key=? AND target_key=? AND conversation_id=?",
                (account_key(source), account_key(target), conversation_id),
            ).fetchone()
        return bool(row and str(row["status"]).startswith("complete"))

    def save(
        self,
        source: str,
        target: str,
        conversation_id: str,
        title: str,
        status: str,
        *,
        share_url: str = "",
        target_url: str = "",
        error: str = "",
    ) -> None:
        with self._lock:
            self._db.execute(
                """
                INSERT INTO items
                    (source_key, target_key, conversation_id, title, status, share_url, target_url, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key, target_key, conversation_id) DO UPDATE SET
                    title=excluded.title, status=excluded.status, share_url=excluded.share_url,
                    target_url=excluded.target_url, error=excluded.error, updated_at=excluded.updated_at
                """,
                (
                    account_key(source), account_key(target), conversation_id, title[:500], status,
                    share_url, target_url, error[:1000], utc_now(),
                ),
            )
            self._db.commit()

    def recent(self, limit: int = 200) -> list[dict[str, str]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT conversation_id, title, status, target_url, error, updated_at "
                "FROM items ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
