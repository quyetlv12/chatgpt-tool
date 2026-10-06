from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "change 2fa community"):
    sys.path.insert(0, str(path))

from fastapi.testclient import TestClient
from db.engine import DatabaseEngine
from db.mysql_sync import MySQLSync
from db.repositories import JobRepository, LiveJournalRepository, SettingsRepository
from db.schema import CURRENT_VERSION
from jobs import TwoFAJob, TwoFAJobManager
import server


class LiveJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "twofa.db"
        self.engine = DatabaseEngine(self.path)
        self.repo = LiveJournalRepository(self.engine)
        self.jobs_repo = JobRepository(self.engine)
        self.manager = TwoFAJobManager(self.jobs_repo, SettingsRepository(self.engine))
        self.job = TwoFAJob(id="live-one", email="synthetic@example.test", password="SYNTHETIC-PASSWORD",
            secret="SYNTHETIC-SECRET", mode="check_only", status="success", account_state="live", login_verified=True,
            plan="plus", usage={"used_percent": 12}, payment_methods=[], created_at=1, finished_at=2,
            logs=["DO-NOT-ARCHIVE-LOGS"], usage_refreshing=True, passkey_preparing=True)
        self.manager.jobs[self.job.id] = self.job
        self.manager.order = [self.job.id]

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def save(self):
        return self.repo.create("Daily Live", self.manager.live_journal_entries())

    def test_only_verified_success_live_rows_are_captured_without_action_flags(self):
        for name, changes in [
            ("unverified", {"login_verified": False}),
            ("pending", {"rotated_pending_verify": True}),
            ("running", {"status": "running"}),
            ("failed-live", {"status": "error"}),
            ("die", {"account_state": "die"}),
        ]:
            job = TwoFAJob(id=name, email=f"{name}@example.test", password="SYNTHETIC", secret="SYNTHETIC",
                status="success", account_state="live", login_verified=True)
            for key, value in changes.items():
                setattr(job, key, value)
            self.manager.jobs[job.id] = job
            self.manager.order.append(job.id)
        entries = self.manager.live_journal_entries()
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["snapshot"]["usage_refreshing"])
        self.assertFalse(entries[0]["snapshot"]["passkey_preparing"])
        self.assertNotIn("DO-NOT-ARCHIVE-LOGS", json.dumps(entries))
        self.job.usage["used_percent"] = 50
        self.assertEqual(entries[0]["snapshot"]["usage"]["used_percent"], 12)
        self.assertTrue(self.job.usage_refreshing, "snapshot must not modify the real job")

    def test_snapshot_credentials_and_view_survive_current_job_change_clear_and_restart(self):
        self.jobs_repo.create({"id": self.job.id, "email": self.job.email, "combo": "[redacted]", "password": self.job.password,
            "secret": self.job.secret, "created_at": 1, "job_type": "twofa_community", "status": "success"})
        journal = self.save()
        self.job.password = "CHANGED"
        self.job.secret = "CHANGED"
        self.job.usage["used_percent"] = 60
        self.jobs_repo.delete_all("twofa_community")
        self.engine.close()
        self.engine = DatabaseEngine(self.path)
        self.repo = LiveJournalRepository(self.engine)
        data = self.repo.get(journal["id"])
        self.assertEqual(data["jobs"][0]["usage"]["used_percent"], 12)
        self.assertEqual(self.repo.raw(journal["id"], self.job.id), "synthetic@example.test|SYNTHETIC-PASSWORD|SYNTHETIC-SECRET")
        self.assertNotIn("SYNTHETIC-PASSWORD", json.dumps(data))
        self.assertNotIn("SYNTHETIC-SECRET", json.dumps(data))
        self.assertEqual(self.repo.list()[0]["account_count"], 1)
        self.assertEqual(self.engine.raw_connection().execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_default_name_validation_empty_and_transaction_rollback(self):
        default = self.repo.create("", self.manager.live_journal_entries())
        self.assertTrue(default["name"].startswith("Nhật ký "))
        for name, entries in [("empty", []), ("a" * 101, self.manager.live_journal_entries()),
                              ("bad\nname", self.manager.live_journal_entries())]:
            with self.assertRaises(ValueError):
                self.repo.create(name, entries)
        with self.assertRaises(RuntimeError):
            with self.engine.transaction():
                self.save()
                raise RuntimeError("rollback")
        self.assertEqual(len(self.repo.list()), 1)

    def test_schema16_upgrade_is_additive_and_preserves_sync_metadata(self):
        with self.engine.transaction() as conn:
            conn.execute("INSERT INTO twofa_sync_meta VALUES('source_id','synthetic-source')")
            conn.execute("DELETE FROM _schema_version WHERE version=?", (CURRENT_VERSION,))
            conn.execute("INSERT INTO _schema_version(version) VALUES(16)")
            conn.execute("DROP TABLE live_journals")
        self.engine.close()
        self.engine = DatabaseEngine(self.path)
        self.repo = LiveJournalRepository(self.engine)
        self.assertEqual(self.repo.list(), [])
        self.assertEqual(self.engine.raw_connection().execute("SELECT value FROM twofa_sync_meta WHERE key='source_id'").fetchone()[0], "synthetic-source")

    def test_journals_enter_offline_outbox_and_new_entities_backfill_once(self):
        sync = MySQLSync(self.engine, Path(self.temp.name) / "missing-config.json")
        sync.prepare()
        journal = self.save()
        _source, batch = sync._batch()
        self.assertEqual([(row[1], row[2]) for row in batch], [("live_journals", journal["id"])])
        sync.sync_once()
        self.assertEqual(sync.status()["pending"], 1)
        with self.engine.transaction() as conn:
            conn.execute("DELETE FROM twofa_sync_outbox")
            conn.execute("UPDATE twofa_sync_meta SET value='1' WHERE key='initialized'")
        sync.prepare()
        self.assertEqual(sync.status()["pending"], 1)
        revision = sync._batch()[1][0][3]
        sync.prepare()
        self.assertEqual(sync._batch()[1][0][3], revision)

    def test_api_auth_no_credentials_in_view_no_requeue_and_independent_raw(self):
        with patch.object(server, "manager", self.manager), patch.object(server, "live_journals", self.repo):
            client = TestClient(server.app)
            for method in ("get", "post"):
                self.assertEqual(getattr(client, method)("/api/live-journals").status_code, 401)
            headers = {"X-Auth-Token": server.auth_token}
            response = client.post("/api/live-journals", headers=headers, json={"name": "Test journal"})
            self.assertEqual(response.status_code, 200)
            journal_id = response.json()["journal"]["id"]
            self.assertNotIn("SYNTHETIC-PASSWORD", response.text)
            self.manager.jobs.clear()
            self.manager.order.clear()
            view = client.get(f"/api/live-journals/{journal_id}", headers=headers)
            self.assertEqual(view.status_code, 200)
            self.assertEqual(view.json()["jobs"][0]["id"], "live-one")
            for text in (view.text, client.get("/api/live-journals", headers=headers).text):
                self.assertNotIn("SYNTHETIC-PASSWORD", text)
                self.assertNotIn("SYNTHETIC-SECRET", text)
                self.assertNotIn("DO-NOT-ARCHIVE-LOGS", text)
            raw_path = f"/api/live-journals/{journal_id}/accounts/live-one/raw"
            self.assertEqual(client.get(raw_path).status_code, 401)
            raw = client.get(raw_path, headers=headers)
            self.assertEqual(raw.text, "synthetic@example.test|SYNTHETIC-PASSWORD|SYNTHETIC-SECRET")
            for reply in (response, view, raw):
                self.assertEqual(reply.headers["cache-control"], "no-store")
                self.assertEqual(reply.headers["x-content-type-options"], "nosniff")
            self.assertEqual(client.get("/api/live-journals/missing", headers=headers).status_code, 404)
            self.assertEqual(client.get(f"/api/live-journals/{journal_id}/accounts/missing/raw", headers=headers).status_code, 404)
            self.assertEqual(self.manager.jobs, {})
            self.assertEqual(self.manager._queue.qsize(), 0)
            self.assertEqual(client.post("/api/live-journals", headers=headers, json={}).status_code, 422)

    def test_storage_failure_has_safe_error_and_keeps_current_list(self):
        with patch.object(server, "manager", self.manager), patch.object(server, "live_journals", self.repo), \
                patch.object(self.repo, "create", side_effect=RuntimeError("SYNTHETIC-PASSWORD")):
            response = TestClient(server.app).post("/api/live-journals", headers={"X-Auth-Token": server.auth_token}, json={})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("SYNTHETIC-PASSWORD", response.text)
        self.assertEqual(self.manager.jobs[self.job.id].password, "SYNTHETIC-PASSWORD")


if __name__ == "__main__":
    unittest.main()
