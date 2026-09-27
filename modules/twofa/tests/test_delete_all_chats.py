from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "change 2fa community"
for path in (str(ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import server  # noqa: E402
from jobs import TwoFAJob, TwoFAJobManager  # noqa: E402
from service import TwoFAFlowError, TwoFAService  # noqa: E402
from session_phase import SessionError, delete_all_chats  # noqa: E402


class _FakeResponse:
    status_code = 204
    text = ""


class _FakeAsyncSession:
    last_patch: dict | None = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def patch(self, url, **kwargs):
        type(self).last_patch = {"url": url, **kwargs}
        return _FakeResponse()


class DeleteAllChatsHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_bulk_visibility_endpoint_with_account_context(self) -> None:
        requests_module = types.ModuleType("curl_cffi.requests")
        requests_module.AsyncSession = _FakeAsyncSession
        curl_module = types.ModuleType("curl_cffi")
        curl_module.requests = requests_module

        with patch.dict(
            sys.modules,
            {"curl_cffi": curl_module, "curl_cffi.requests": requests_module},
        ):
            await delete_all_chats(
                access_token="test-token",
                account_id="acct-test",
                cookies=[{"name": "session", "value": "test-cookie"}],
            )

        request = _FakeAsyncSession.last_patch or {}
        self.assertEqual(request["url"], "https://chatgpt.com/backend-api/conversations")
        self.assertEqual(request["json"], {"is_visible": False})
        self.assertEqual(request["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(request["headers"]["ChatGPT-Account-Id"], "acct-test")
        self.assertIn("session=test-cookie", request["headers"]["Cookie"])

    async def test_rejects_invalid_identity_before_network(self) -> None:
        with self.assertRaises(SessionError):
            await delete_all_chats(access_token="", account_id="acct-test")
        with self.assertRaises(SessionError):
            await delete_all_chats(access_token="test-token", account_id="bad\nid")

    async def test_upstream_error_does_not_expose_response_body(self) -> None:
        class ErrorResponse(_FakeResponse):
            status_code = 500
            text = "private response with test-token"

        class ErrorSession(_FakeAsyncSession):
            async def patch(self, url, **kwargs):
                type(self).last_patch = {"url": url, **kwargs}
                return ErrorResponse()

        requests_module = types.ModuleType("curl_cffi.requests")
        requests_module.AsyncSession = ErrorSession
        curl_module = types.ModuleType("curl_cffi")
        curl_module.requests = requests_module
        with patch.dict(
            sys.modules,
            {"curl_cffi": curl_module, "curl_cffi.requests": requests_module},
        ):
            with self.assertRaises(SessionError) as raised:
                await delete_all_chats(
                    access_token="test-token",
                    account_id="acct-test",
                )

        self.assertEqual(str(raised.exception), "delete chats HTTP 500")


class DeleteAllChatsServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reauthenticates_then_calls_delete_once(self) -> None:
        login_calls: list[dict] = []
        delete_calls: list[dict] = []

        async def login_fn(**kwargs):
            login_calls.append(kwargs)
            return {
                "accessToken": "test-token",
                "__cookies": [{"name": "session", "value": "test-cookie"}],
                "account": {"id": "acct-test"},
            }

        async def delete_fn(**kwargs):
            delete_calls.append(kwargs)

        service = TwoFAService(
            login_fn=login_fn,
            delete_chats_fn=delete_fn,
            login_attempts=1,
        )
        await service.delete_all_chats(
            email="demo@example.com",
            password="password",
            secret="TESTSECRET",
            timeout=30,
            log=lambda _message: None,
        )

        self.assertEqual(len(login_calls), 1)
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0]["account_id"], "acct-test")

    async def test_returns_only_safe_error(self) -> None:
        async def login_fn(**_kwargs):
            return {"accessToken": "test-token", "account": {"id": "acct-test"}}

        async def delete_fn(**_kwargs):
            raise RuntimeError("private response with test-token")

        logs: list[str] = []
        service = TwoFAService(
            login_fn=login_fn,
            delete_chats_fn=delete_fn,
            login_attempts=1,
        )
        with self.assertRaises(TwoFAFlowError) as raised:
            await service.delete_all_chats(
                email="demo@example.com",
                password="password",
                secret="TESTSECRET",
                timeout=30,
                log=logs.append,
            )

        self.assertEqual(str(raised.exception), "Không thể xóa dữ liệu chat; vui lòng thử lại")
        self.assertFalse(any("test-token" in line for line in logs))


class _SettingsRepo:
    def list(self, _prefix=None):
        return {}


class _JobRepo:
    def __init__(self) -> None:
        self.updated: list = []
        self.logs: list = []

    def list_all(self):
        return []

    def get_logs(self, _job_id):
        return []

    def update_status(self, *args, **kwargs):
        self.updated.append((args, kwargs))

    def append_log(self, *args, **kwargs):
        self.logs.append((args, kwargs))


class _DeleteService:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None
        self.error: Exception | None = None

    async def delete_all_chats(self, **kwargs):
        self.calls.append(kwargs)
        if self.entered:
            self.entered.set()
        if self.release:
            await self.release.wait()
        if self.error:
            raise self.error


class DeleteAllChatsManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.repo = _JobRepo()
        self.service = _DeleteService()
        self.manager = TwoFAJobManager(self.repo, _SettingsRepo(), service=self.service)
        self.job = TwoFAJob(
            id="live-job",
            email="demo@example.com",
            password="password",
            secret="TESTSECRET",
            mode="check_only",
            status="success",
            account_state="live",
            login_verified=True,
        )
        self.manager.jobs[self.job.id] = self.job
        self.manager.order.append(self.job.id)

    async def test_keeps_job_and_database_unchanged(self) -> None:
        before = self.job.snapshot()

        result = await self.manager.delete_all_chats(self.job.id)

        self.assertEqual(result, before)
        self.assertEqual(self.job.snapshot(), before)
        self.assertEqual(self.repo.updated, [])
        self.assertEqual(self.repo.logs, [])
        self.assertEqual(len(self.service.calls), 1)

    async def test_rejects_ineligible_or_conflicting_work(self) -> None:
        self.job.account_state = "unknown"
        with self.assertRaises(ValueError):
            await self.manager.delete_all_chats(self.job.id)
        self.job.account_state = "live"
        self.job.status = "running"
        with self.assertRaises(ValueError):
            await self.manager.delete_all_chats(self.job.id)
        self.job.status = "success"
        self.job.usage_refreshing = True
        with self.assertRaises(ValueError):
            await self.manager.delete_all_chats(self.job.id)

    async def test_flag_blocks_conflicting_actions_and_always_clears(self) -> None:
        self.service.entered = asyncio.Event()
        self.service.release = asyncio.Event()
        task = asyncio.create_task(self.manager.delete_all_chats(self.job.id))
        await self.service.entered.wait()

        self.assertTrue(self.job.snapshot()["chat_deleting"])
        with self.assertRaises(ValueError):
            self.manager.recheck(self.job.id)
        with self.assertRaises(ValueError):
            self.manager.enqueue_change_2fa(self.job.id)
        with self.assertRaises(ValueError):
            self.manager.delete(self.job.id)
        with self.assertRaises(ValueError):
            await self.manager.refresh_usage(self.job.id)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.job.chat_deleting)


class _ApiManager:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def delete_all_chats(self, job_id: str) -> dict:
        self.calls.append(job_id)
        if job_id == "missing":
            raise KeyError(job_id)
        if job_id == "blocked":
            raise ValueError("Tài khoản chưa đủ điều kiện")
        if job_id == "upstream":
            raise TwoFAFlowError("Không thể xóa dữ liệu chat; vui lòng thử lại")
        return {
            "id": job_id,
            "email": "demo@example.com",
            "status": "success",
            "account_state": "live",
            "chat_deleting": False,
        }


class DeleteAllChatsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_manager = server.manager
        self.manager = _ApiManager()
        server.manager = self.manager
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        server.manager = self.original_manager

    def request(self, job_id="live-job", *, token=True, confirm="DELETE_ALL_CHATS"):
        headers = {"X-Auth-Token": server.auth_token} if token else {}
        return self.client.post(
            f"/api/jobs/{job_id}/delete-chats",
            headers=headers,
            json={"confirm": confirm},
        )

    def test_requires_auth_and_exact_confirmation(self) -> None:
        self.assertEqual(self.request(token=False).status_code, 401)
        self.assertEqual(self.request(confirm="delete").status_code, 422)
        self.assertEqual(self.manager.calls, [])

    def test_returns_safe_snapshot(self) -> None:
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["job"]["chat_deleting"])
        self.assertNotIn("password", response.json()["job"])
        self.assertNotIn("secret", response.json()["job"])

    def test_maps_missing_conflict_and_upstream_errors(self) -> None:
        self.assertEqual(self.request("missing").status_code, 404)
        self.assertEqual(self.request("blocked").status_code, 409)
        response = self.request("upstream")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"],
            "Không thể xóa dữ liệu chat; vui lòng thử lại",
        )


if __name__ == "__main__":
    unittest.main()
