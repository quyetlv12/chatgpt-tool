from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "change 2fa community"
for path in (str(ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from jobs import JobPersistenceError, TwoFAJob, TwoFAJobManager  # noqa: E402


class _SettingsRepo:
    def list(self, _prefix=None):
        return {}


class _JobRepo:
    def __init__(self) -> None:
        self.updated: list[tuple[str, str, dict]] = []
        self.fail_update = False

    def list_all(self):
        return []

    def get_logs(self, _job_id: str):
        return []

    def update_status(self, job_id: str, status: str, **kwargs) -> None:
        if self.fail_update:
            raise RuntimeError("simulated persistence failure")
        self.updated.append((job_id, status, kwargs))

    def append_log(self, _job_id: str, _line: str) -> None:
        return None


class _CheckOnlyService:
    def __init__(self) -> None:
        self.check_calls: list[dict] = []
        self.rotate_calls: list[dict] = []

    async def check(self, **kwargs):
        self.check_calls.append(kwargs)
        return SimpleNamespace(
            secret=kwargs["secret"],
            login_verified=True,
            account_state="live",
            plan="plus",
            plan_source="session",
            usage={"used_percent": 12.0, "remaining_percent": 88.0},
        )

    async def rotate(self, **kwargs):
        self.rotate_calls.append(kwargs)
        raise AssertionError("recheck must never rotate 2FA")


class JobRecheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _JobRepo()
        self.service = _CheckOnlyService()
        self.manager = TwoFAJobManager(
            self.repo,
            _SettingsRepo(),
            service=self.service,
        )
        self.job = TwoFAJob(
            id="successful-change",
            email="live@example.com",
            password="password",
            secret="CURRENTSECRET",
            mode="change_2fa",
            status="success",
            error="stale error",
            error_kind="technical_error",
            account_state="live",
            plan="plus",
            plan_source="session",
            usage={"used_percent": 44.0, "remaining_percent": 56.0},
            rotated_pending_verify=True,
            login_verified=True,
            retry_count=3,
            started_at=100.0,
            finished_at=200.0,
        )
        self.manager.jobs[self.job.id] = self.job
        self.manager.order.append(self.job.id)

    async def test_recheck_queues_same_job_in_check_only_mode_and_clears_stale_result(self) -> None:
        snapshot = self.manager.recheck(self.job.id)

        self.assertEqual(snapshot["mode"], "check_only")
        self.assertEqual(snapshot["status"], "queued")
        self.assertIsNone(snapshot["error"])
        self.assertIsNone(snapshot["error_kind"])
        self.assertEqual(snapshot["account_state"], "unknown")
        self.assertIsNone(snapshot["plan"])
        self.assertIsNone(snapshot["plan_source"])
        self.assertIsNone(snapshot["usage"])
        self.assertFalse(snapshot["rotated_pending_verify"])
        self.assertFalse(snapshot["login_verified"])
        self.assertEqual(snapshot["retry_count"], 0)
        self.assertIsNone(snapshot["started_at"])
        self.assertIsNone(snapshot["finished_at"])
        self.assertEqual(self.job.password, "password")
        self.assertEqual(self.job.secret, "CURRENTSECRET")
        self.assertNotIn("password", snapshot)
        self.assertNotIn("secret", snapshot)
        self.assertEqual(self.manager._queue.get_nowait(), self.job.id)

        job_id, status, payload = self.repo.updated[-1]
        self.assertEqual((job_id, status), (self.job.id, "queued"))
        self.assertEqual(payload["password"], "password")
        self.assertEqual(payload["secret"], "CURRENTSECRET")
        persisted_state = json.loads(payload["account_check"])
        self.assertEqual(persisted_state["mode"], "check_only")
        self.assertEqual(persisted_state["account_state"], "unknown")
        self.assertIsNone(persisted_state["usage"])

    async def test_persistence_failure_rolls_back_every_mutated_field(self) -> None:
        expected = {
            field: getattr(self.job, field)
            for field in (
                "mode",
                "status",
                "error",
                "error_kind",
                "account_state",
                "plan",
                "plan_source",
                "usage",
                "rotated_pending_verify",
                "login_verified",
                "retry_count",
                "started_at",
                "finished_at",
            )
        }
        self.repo.fail_update = True

        with self.assertRaises(JobPersistenceError):
            self.manager.recheck(self.job.id)

        self.assertEqual(
            {field: getattr(self.job, field) for field in expected},
            expected,
        )
        self.assertTrue(self.manager._queue.empty())

    async def test_recheck_rejects_ineligible_or_busy_jobs(self) -> None:
        for status in ("queued", "running", "error", "cancelled"):
            with self.subTest(status=status):
                self.job.status = status
                with self.assertRaises(ValueError):
                    self.manager.recheck(self.job.id)

        self.job.status = "success"
        self.job.usage_refreshing = True
        with self.assertRaisesRegex(ValueError, "Usage"):
            self.manager.recheck(self.job.id)

    async def test_requeued_worker_checks_account_without_rotating_2fa(self) -> None:
        self.manager.recheck(self.job.id)

        await self.manager._run(self.job)

        self.assertEqual(len(self.service.check_calls), 1)
        self.assertEqual(self.service.rotate_calls, [])
        self.assertEqual(self.job.status, "success")
        self.assertEqual(self.job.account_state, "live")
        self.assertEqual(self.job.plan, "plus")
        self.assertEqual(self.job.usage["remaining_percent"], 88.0)


if __name__ == "__main__":
    unittest.main()
