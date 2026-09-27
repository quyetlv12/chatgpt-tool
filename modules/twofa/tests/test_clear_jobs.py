from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "change 2fa community"
for path in (str(ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from jobs import TwoFAJob, TwoFAJobManager  # noqa: E402
from password_jobs import PasswordJob, PasswordJobManager  # noqa: E402


class _SettingsRepo:
    def list(self, _prefix=None):
        return {}


class _SlowDeleteRepo:
    def __init__(self, delay: float = 0.08, deleted: int = 3) -> None:
        self.delay = delay
        self.deleted = deleted

    def list_all(self):
        return []

    def get_logs(self, _job_id: str):
        return []

    def delete_all(self, _job_type: str):
        time.sleep(self.delay)
        return self.deleted


async def _wait_for_heartbeat() -> None:
    await asyncio.sleep(0.01)


class ClearJobsTests(unittest.IsolatedAsyncioTestCase):
    async def test_twofa_clear_keeps_event_loop_responsive(self) -> None:
        repo = _SlowDeleteRepo()
        manager = TwoFAJobManager(repo, _SettingsRepo())
        job = TwoFAJob(
            id="terminal-twofa",
            email="terminal@example.test",
            password="password",
            secret="ABCDEFGHIJKLMNOP",
            status="success",
        )
        manager.jobs[job.id] = job
        manager.order.append(job.id)
        heartbeat = asyncio.create_task(_wait_for_heartbeat())

        deleted = await manager.clear_async()

        await heartbeat
        self.assertEqual(deleted, 3)
        self.assertFalse(heartbeat.cancelled())
        self.assertEqual(manager.jobs, {})

    async def test_password_clear_keeps_event_loop_responsive(self) -> None:
        repo = _SlowDeleteRepo()
        manager = PasswordJobManager(repo, _SettingsRepo())
        job = PasswordJob(
            id="terminal-password",
            email="terminal@example.test",
            password="password",
            secret="ABCDEFGHIJKLMNOP",
            pending_password=None,
            status="success",
            phase="verified",
            login_verified=True,
        )
        manager.jobs[job.id] = job
        manager.order.append(job.id)
        heartbeat = asyncio.create_task(_wait_for_heartbeat())

        deleted = await manager.clear_async()

        await heartbeat
        self.assertEqual(deleted, 3)
        self.assertFalse(heartbeat.cancelled())
        self.assertEqual(manager.jobs, {})


if __name__ == "__main__":
    unittest.main()
