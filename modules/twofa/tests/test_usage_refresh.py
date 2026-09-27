from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "change 2fa community"
for path in (str(ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from jobs import JobPersistenceError, TwoFAJob, TwoFAJobManager  # noqa: E402
from service import TwoFAFlowError, TwoFAService  # noqa: E402


USAGE = {
    "used_percent": 28.0,
    "remaining_percent": 72.0,
    "limit_window_seconds": 604800,
}


class _SettingsRepo:
    def list(self, _prefix=None):
        return {}


class _JobRepo:
    def __init__(self) -> None:
        self.updated: list[tuple[str, str, dict]] = []
        self.logs: list[tuple[str, str]] = []
        self.fail_update = False

    def list_all(self):
        return []

    def get_logs(self, _job_id: str):
        return []

    def update_status(self, job_id: str, status: str, **kwargs) -> None:
        if self.fail_update:
            raise RuntimeError("simulated persistence failure")
        self.updated.append((job_id, status, kwargs))

    def append_log(self, job_id: str, line: str) -> None:
        self.logs.append((job_id, line))


class _UsageService:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def refresh_usage(self, **kwargs):
        self.calls.append(kwargs)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return dict(USAGE)


class TwoFAServiceRefreshUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_usage_reauthenticates_and_returns_usage_only(self) -> None:
        login_calls: list[dict] = []
        usage_calls: list[dict] = []

        async def login_fn(**kwargs):
            login_calls.append(kwargs)
            return {
                "accessToken": "eyJ.demo.token",
                "__cookies": [{"name": "session", "value": "cookie-value"}],
                "account": {"id": "acct-demo"},
            }

        async def usage_fn(**kwargs):
            usage_calls.append(kwargs)
            return dict(USAGE)

        service = TwoFAService(login_fn=login_fn, usage_fn=usage_fn, login_attempts=1)

        result = await service.refresh_usage(
            email="demo@example.com",
            password="password",
            secret="JBSWY3DPEHPK3PXP",
            timeout=30,
            log=lambda _message: None,
        )

        self.assertEqual(result["used_percent"], 28.0)
        self.assertEqual(len(login_calls), 1)
        self.assertEqual(len(usage_calls), 1)
        self.assertEqual(usage_calls[0]["account_id"], "acct-demo")

    async def test_refresh_usage_raises_safe_error_when_usage_is_still_unavailable(self) -> None:
        logs: list[str] = []

        async def login_fn(**_kwargs):
            return {"accessToken": "eyJ.demo.token", "account": {"id": "acct-demo"}}

        async def usage_fn(**_kwargs):
            raise RuntimeError("request failed with eyJ.secret.token")

        service = TwoFAService(login_fn=login_fn, usage_fn=usage_fn, login_attempts=1)

        with self.assertRaisesRegex(TwoFAFlowError, "Chưa đọc được Usage"):
            await service.refresh_usage(
                email="demo@example.com",
                password="password",
                secret="JBSWY3DPEHPK3PXP",
                timeout=30,
                log=logs.append,
            )

        self.assertFalse(any("eyJ.secret.token" in line for line in logs))

    async def test_refresh_usage_does_not_expose_login_error_details(self) -> None:
        async def login_fn(**_kwargs):
            raise RuntimeError("login response contained eyJ.secret.token")

        service = TwoFAService(login_fn=login_fn, login_attempts=1)

        with self.assertRaises(TwoFAFlowError) as raised:
            await service.refresh_usage(
                email="demo@example.com",
                password="password",
                secret="JBSWY3DPEHPK3PXP",
                timeout=30,
                log=lambda _message: None,
            )

        self.assertEqual(str(raised.exception), "Không thể đăng nhập lại để đọc Usage")
        self.assertNotIn("eyJ.secret.token", str(raised.exception))


class UsageRefreshManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _JobRepo()
        self.service = _UsageService()
        self.manager = TwoFAJobManager(
            self.repo,
            _SettingsRepo(),
            service=self.service,
        )
        self.job = TwoFAJob(
            id="live-no-usage",
            email="live@example.com",
            password="password",
            secret="JBSWY3DPEHPK3PXP",
            mode="check_only",
            status="success",
            account_state="live",
            login_verified=True,
            usage=None,
        )
        self.manager.jobs[self.job.id] = self.job
        self.manager.order.append(self.job.id)

    async def test_refresh_usage_persists_result_without_changing_job_success(self) -> None:
        snapshot = await self.manager.refresh_usage(self.job.id)

        self.assertEqual(snapshot["status"], "success")
        self.assertEqual(snapshot["usage"]["remaining_percent"], 72.0)
        self.assertFalse(snapshot["usage_refreshing"])
        self.assertNotIn("password", snapshot)
        self.assertNotIn("secret", snapshot)
        self.assertEqual(len(self.service.calls), 1)
        self.assertEqual(self.repo.updated[-1][:2], (self.job.id, "success"))
        persisted_state = json.loads(self.repo.updated[-1][2]["account_check"])
        self.assertEqual(persisted_state["usage"]["used_percent"], 28.0)

    async def test_refresh_failure_keeps_verified_job_and_unlocks_button(self) -> None:
        self.service.error = TwoFAFlowError(
            "Chưa đọc được Usage; vui lòng thử lại",
            account_state="live",
        )

        with self.assertRaises(TwoFAFlowError):
            await self.manager.refresh_usage(self.job.id)

        self.assertEqual(self.job.status, "success")
        self.assertEqual(self.job.account_state, "live")
        self.assertIsNone(self.job.usage)
        self.assertFalse(self.job.usage_refreshing)

    async def test_persistence_failure_rolls_back_usage_and_unlocks_button(self) -> None:
        self.repo.fail_update = True

        with self.assertRaises(JobPersistenceError):
            await self.manager.refresh_usage(self.job.id)

        self.assertEqual(self.job.status, "success")
        self.assertIsNone(self.job.usage)
        self.assertFalse(self.job.usage_refreshing)

    async def test_duplicate_refresh_is_rejected_while_first_request_is_running(self) -> None:
        self.service.entered = asyncio.Event()
        self.service.release = asyncio.Event()
        first = asyncio.create_task(self.manager.refresh_usage(self.job.id))
        await self.service.entered.wait()

        with self.assertRaisesRegex(ValueError, "đang được đọc lại"):
            await self.manager.refresh_usage(self.job.id)

        self.service.release.set()
        await first

    async def test_refresh_rejects_ineligible_account_or_existing_usage(self) -> None:
        self.job.account_state = "unknown"
        with self.assertRaises(ValueError):
            await self.manager.refresh_usage(self.job.id)

        self.job.account_state = "live"
        self.job.usage = dict(USAGE)
        with self.assertRaises(ValueError):
            await self.manager.refresh_usage(self.job.id)


if __name__ == "__main__":
    unittest.main()
