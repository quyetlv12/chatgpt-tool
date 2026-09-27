from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "change 2fa community"
for path in (str(ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from jobs import TwoFAJobManager  # noqa: E402
from service import RotationResult  # noqa: E402


class _SettingsRepo:
    def __init__(self, concurrency: int = 2) -> None:
        self.values = {"twofa.max_concurrent": concurrency}

    def list(self, _prefix=None):
        return dict(self.values)

    def set(self, key, value) -> None:
        self.values[key] = value


class _JobRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.running_failures = 0
        self.terminal_failures = 0

    def list_all(self):
        return []

    def create(self, row) -> str:
        self.rows[row["id"]] = dict(row)
        return row["id"]

    def update_status(self, job_id: str, status: str, **kwargs) -> None:
        if status == "running" and self.running_failures:
            self.running_failures -= 1
            raise RuntimeError("simulated running persistence failure")
        if status in {"success", "error", "cancelled"} and self.terminal_failures:
            self.terminal_failures -= 1
            raise RuntimeError("simulated terminal persistence failure")
        self.rows[job_id]["status"] = status
        self.rows[job_id].update(kwargs)

    def append_log(self, _job_id: str, _line: str) -> None:
        return None

    def get_logs(self, _job_id: str):
        return []


class _Service:
    async def check(self, **kwargs) -> RotationResult:
        if kwargs["email"].startswith("service-error"):
            raise RuntimeError("simulated service failure")
        await asyncio.sleep(0)
        return RotationResult(
            secret=kwargs["secret"],
            login_verified=True,
            account_state="live",
        )


async def _wait_for(predicate, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not reached before timeout")
        await asyncio.sleep(0.01)


class WorkerResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _JobRepo()
        self.manager = TwoFAJobManager(
            self.repo,
            _SettingsRepo(),
            service=_Service(),
        )

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()

    async def test_pre_run_persistence_failure_does_not_kill_worker_pool(self) -> None:
        self.repo.running_failures = 1
        self.manager.start()

        created = self.manager.add(
            [
                "first@example.test|pw|ABCDEFGHIJKLMNOP",
                "second@example.test|pw|ABCDEFGHIJKLMNOP",
            ],
            "check_only",
        )

        await _wait_for(
            lambda: all(
                self.manager.jobs[item["id"]].status in {"success", "error"}
                for item in created
            )
        )

        first, second = (self.manager.jobs[item["id"]] for item in created)
        self.assertEqual(first.status, "error")
        self.assertEqual(first.error_kind, "technical_error")
        self.assertEqual(second.status, "success")
        self.assertEqual(self.manager.worker_health()["active"], 2)
        self.assertEqual(self.manager.worker_health()["persistence_failures"], 1)

    async def test_terminal_persistence_failure_is_contained(self) -> None:
        self.repo.terminal_failures = 1
        self.manager.start()

        created = self.manager.add(
            [
                "service-error@example.test|pw|ABCDEFGHIJKLMNOP",
                "healthy@example.test|pw|ABCDEFGHIJKLMNOP",
            ],
            "check_only",
        )

        await _wait_for(
            lambda: all(
                self.manager.jobs[item["id"]].status in {"success", "error"}
                for item in created
            )
        )

        failed, healthy = (self.manager.jobs[item["id"]] for item in created)
        self.assertEqual(failed.status, "error")
        self.assertIn("không lưu được trạng thái", failed.error.lower())
        self.assertEqual(healthy.status, "success")
        self.assertEqual(self.manager.worker_health()["active"], 2)

    async def test_success_persistence_failure_becomes_visible_error(self) -> None:
        self.repo.terminal_failures = 1
        self.manager.start()

        created = self.manager.add(
            ["healthy@example.test|pw|ABCDEFGHIJKLMNOP"],
            "check_only",
        )

        await _wait_for(
            lambda: self.manager.jobs[created[0]["id"]].status in {"success", "error"}
        )

        job = self.manager.jobs[created[0]["id"]]
        self.assertEqual(job.status, "error")
        self.assertIn("không lưu được trạng thái", job.error.lower())
        self.assertEqual(self.manager.worker_health()["active"], 2)

    async def test_unexpected_worker_cancellation_is_replaced(self) -> None:
        self.manager.start()
        original = list(self.manager._active_workers())

        original[0].cancel()
        await asyncio.gather(original[0], return_exceptions=True)
        await _wait_for(lambda: self.manager.worker_health()["active"] == 2)

        health = self.manager.worker_health()
        self.assertEqual(health["configured"], 2)
        self.assertEqual(health["active"], 2)
        self.assertEqual(health["restarts"], 1)
        self.assertFalse(health["degraded"])

    async def test_shutdown_does_not_respawn_workers(self) -> None:
        self.manager.start()

        await self.manager.shutdown()
        await asyncio.sleep(0)

        health = self.manager.worker_health()
        self.assertEqual(health["active"], 0)
        self.assertFalse(health["started"])

    async def test_planned_resize_does_not_count_as_worker_failure(self) -> None:
        self.manager.start()

        await self.manager.update_settings({"twofa.max_concurrent": 1})
        await _wait_for(lambda: self.manager.worker_health()["active"] == 1)
        await self.manager.update_settings({"twofa.max_concurrent": 3})
        await _wait_for(lambda: self.manager.worker_health()["active"] == 3)

        health = self.manager.worker_health()
        self.assertEqual(health["restarts"], 0)
        self.assertFalse(health["degraded"])

    async def test_started_manager_rejects_cross_thread_mutation(self) -> None:
        self.manager.start()

        with self.assertRaisesRegex(RuntimeError, "event-loop thread"):
            await asyncio.to_thread(
                self.manager.add,
                ["threaded@example.test|pw|ABCDEFGHIJKLMNOP"],
                "check_only",
            )

        self.assertEqual(self.manager.jobs, {})


if __name__ == "__main__":
    unittest.main()
