from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "change 2fa community"
for path in (str(ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from db.engine import DatabaseEngine  # noqa: E402
from db.repositories import JobRepository  # noqa: E402
from db.schema import CURRENT_VERSION  # noqa: E402
from jobs import TwoFAJob, TwoFAJobManager  # noqa: E402
from service import RotationResult  # noqa: E402


class _SettingsRepo:
    def list(self, _prefix=None):
        return {}


class _MemoryJobRepo:
    def list_all(self):
        return []


class TwoFAOutputOrderingTests(unittest.TestCase):
    def test_verified_output_is_sorted_by_completion_time_ascending(self) -> None:
        manager = TwoFAJobManager(_MemoryJobRepo(), _SettingsRepo())
        later = TwoFAJob(
            id="later",
            email="later@example.com",
            password="password-later",
            secret="SECRET-LATER",
            mode="change_2fa",
            status="success",
            login_verified=True,
            created_at=1,
            finished_at=200,
        )
        earlier = TwoFAJob(
            id="earlier",
            email="earlier@example.com",
            password="password-earlier",
            secret="SECRET-EARLIER",
            mode="change_2fa",
            status="success",
            login_verified=True,
            created_at=2,
            finished_at=100,
        )
        manager.jobs = {later.id: later, earlier.id: earlier}
        manager.order = [later.id, earlier.id]

        self.assertEqual(
            manager.output(),
            [
                "earlier@example.com|password-earlier|SECRET-EARLIER",
                "later@example.com|password-later|SECRET-LATER",
            ],
        )


class TwoFAHistoryPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = DatabaseEngine(Path(self.temp_dir.name) / "history.db")
        self.repo = JobRepository(self.engine)

    def tearDown(self) -> None:
        self.engine.close()
        self.temp_dir.cleanup()

    def _create_job(
        self,
        job_id: str,
        *,
        mode: str = "change_2fa",
        status: str = "queued",
        finished_at: float | None = None,
    ) -> None:
        state = json.dumps({
            "mode": mode,
            "login_verified": status == "success",
            "account_state": "live",
        })
        self.repo.create({
            "id": job_id,
            "email": f"{job_id}@example.com",
            "combo": "[redacted]",
            "mail_mode": "none",
            "status": status,
            "password": f"password-{job_id}",
            "secret": f"SECRET-{job_id}",
            "account_check": state,
            "created_at": 1,
            "finished_at": finished_at,
            "job_type": "twofa_community",
        })

    def test_schema_contains_persistent_history_table(self) -> None:
        self.assertGreaterEqual(CURRENT_VERSION, 14)
        table = self.engine.raw_connection().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='twofa_history'"
        ).fetchone()
        self.assertIsNotNone(table)

    def test_existing_v13_database_migrates_to_history_schema(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy-v13.db"
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            "CREATE TABLE _schema_version (version INTEGER PRIMARY KEY, description TEXT)"
        )
        connection.execute(
            "INSERT INTO _schema_version (version, description) VALUES (13, 'legacy')"
        )
        connection.commit()
        connection.close()

        migrated = DatabaseEngine(legacy_path)
        try:
            version = migrated.raw_connection().execute(
                "SELECT MAX(version) FROM _schema_version"
            ).fetchone()[0]
            table = migrated.raw_connection().execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='twofa_history'"
            ).fetchone()
            self.assertEqual(version, CURRENT_VERSION)
            self.assertIsNotNone(table)
        finally:
            migrated.close()

    def test_atomic_success_record_survives_job_deletion(self) -> None:
        self._create_job("job-1")

        self.repo.complete_twofa_success(
            job_id="job-1",
            email="job-1@example.com",
            password="new-password",
            secret="NEW-SECRET",
            account_check=json.dumps({"mode": "change_2fa", "login_verified": True}),
            changed_at=123.5,
        )
        self.repo.delete("job-1")

        self.assertEqual(
            self.repo.list_twofa_history(),
            [{
                "id": 1,
                "job_id": "job-1",
                "email": "job-1@example.com",
                "password": "new-password",
                "secret": "NEW-SECRET",
                "changed_at": 123.5,
            }],
        )

    def test_manager_backfills_existing_successful_change_jobs_once(self) -> None:
        self._create_job("existing", status="success", finished_at=77.0)
        self._create_job("check-only", mode="check_only", status="success", finished_at=88.0)

        manager = TwoFAJobManager(self.repo, _SettingsRepo())
        manager_again = TwoFAJobManager(self.repo, _SettingsRepo())

        self.assertEqual(len(manager.twofa_history()), 1)
        self.assertEqual(len(manager_again.twofa_history()), 1)
        self.assertEqual(manager.twofa_history()[0]["raw"], "existing@example.com|password-existing|SECRET-existing")


class _SuccessfulRotationService:
    async def rotate(self, **kwargs) -> RotationResult:
        await kwargs["checkpoint"]("NEWSECRET")
        return RotationResult(
            secret="NEWSECRET",
            login_verified=True,
            account_state="live",
            plan="plus",
        )


class TwoFARealtimeCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_event_and_output_both_contain_completed_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = DatabaseEngine(Path(directory) / "realtime.db")
            manager = TwoFAJobManager(
                JobRepository(engine),
                _SettingsRepo(),
                service=_SuccessfulRotationService(),
            )
            events = manager.subscribe()
            manager.start()
            try:
                created = manager.add(
                    ["live@example.com|password|OLDSECRET"],
                    mode="change_2fa",
                )[0]
                while True:
                    payload = await asyncio.wait_for(events.get(), timeout=1)
                    if payload.get("job", {}).get("status") == "success":
                        break

                self.assertTrue(payload["job"]["login_verified"])
                self.assertEqual(
                    manager.output(),
                    ["live@example.com|password|NEWSECRET"],
                )
                self.assertEqual(
                    manager.twofa_history()[0]["raw"],
                    "live@example.com|password|NEWSECRET",
                )
                self.assertEqual(payload["job"]["id"], created["id"])
            finally:
                manager.unsubscribe(events)
                await manager.shutdown()
                engine.close()


if __name__ == "__main__":
    unittest.main()
