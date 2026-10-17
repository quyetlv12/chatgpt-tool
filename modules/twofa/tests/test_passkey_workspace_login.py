"""Bulk passkey HTTP-login integration; synthetic accounts, no WebAuthn/network."""
import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "change 2fa community"))

from passkey_jobs import PasskeyJobManager
from passkey_service import PasskeyHandoffs
from service import TwoFAService
from test_personal_workspace_login import LoginSession, WORKSPACES

URL = "https://auth.openai.com/passkey-enroll?origin_app_name=ChatGPT&mfa_token=synthetic-state"
PERSONAL_LOG = "[auth] Đã chọn Personal account"


@contextmanager
def http_fixture(sessions, flow):
    transport = AsyncMock()
    transport.cookies = Mock()
    transport.__aenter__.return_value = transport
    transport.post.return_value = SimpleNamespace(status_code=200, json=lambda: {"state_token": "synthetic-state"})

    def consume(client, _url, _log):
        client.cookies.set("__Secure-next-auth.session-token", "synthetic-session", domain="chatgpt.com", secure=True)
        return True

    with (
        patch("request_phase._create_session", side_effect=sessions),
        patch("session_phase._resolve_login_flow", return_value=flow),
        patch("request_phase._step_csrf", return_value="fixture-csrf"),
        patch("request_phase._get_sentinel_token", return_value="fixture-sentinel"),
        patch("request_phase._consume_callback", side_effect=consume),
        patch("totp_helper.generate_code", return_value="123456"),
        patch("curl_cffi.requests.AsyncSession", return_value=transport),
    ):
        yield transport


class PasskeyWorkspaceLoginTests(IsolatedAsyncioTestCase):
    async def test_personal_milestone_never_forwards_raw_login_data(self):
        async def login(**kwargs):
            for message in (
                "[session-req] Đã chọn Personal account",
                "[session-req] Đã chọn Personal account private-workspace-id",
                "[session-req] callback secret-token",
            ):
                kwargs["log"](message)
            return {"accessToken": "synthetic-token"}

        manager = PasskeyJobManager(service=TwoFAService(login_fn=login, login_attempts=1))
        created = manager.add(["account@example.test|fixture-password|FIXTURE"])
        with patch("passkey_service.prepare_passkey_url", new=AsyncMock(return_value=URL)):
            manager.start()
            try:
                await asyncio.wait_for(manager._queue.join(), 1)
            finally:
                await manager.shutdown()
        logs = manager.logs(created[0]["id"])
        self.assertEqual(sum(line.endswith(PERSONAL_LOG) for line in logs), 1)
        self.assertNotIn("private-workspace-id", "\n".join(logs))
        self.assertNotIn("secret-token", "\n".join(logs))

    async def run_batch(self, sessions, flow):
        service = TwoFAService(login_attempts=1)
        manager = PasskeyJobManager(service=service, max_concurrent=len(sessions))
        created = manager.add([f"account{i}@example.test|fixture-password|FIXTURE" for i in range(len(sessions))])
        forbidden = AsyncMock(side_effect=AssertionError("Passkey must not inspect or mutate accounts"))
        with http_fixture(sessions, flow) as transport, patch.object(service, "_inspect_account", new=forbidden):
            manager.start()
            try:
                await asyncio.wait_for(manager._queue.join(), 5)
            finally:
                await manager.shutdown()
        forbidden.assert_not_awaited()
        self.assertTrue(all(session.closed for session in sessions))
        public = json.dumps(manager.snapshots(), ensure_ascii=False)
        for private in ("fixture-password", "FIXTURE", "synthetic-token", "synthetic-state", "personal-fixture", "edu-fixture", "private-upstream-body", "synthetic-session"):
            self.assertNotIn(private, public)
        for session in sessions:
            if session.session_payload and "accessToken" in session.session_payload:
                self.assertNotIn(session.session_payload["accessToken"], public)
                self.assertNotIn(session.session_payload["account"]["id"], public)
        return manager, created, transport

    async def test_bulk_workspace_cookie_and_dump_login_precede_official_handoff(self):
        for flow in ("anti409", "legacy"):
            for chooser in ("direct", "redirect", "reauthorize", "no-continuation"):
                for missing_cookie in (False, True):
                    with self.subTest(flow=flow, chooser=chooser, missing_cookie=missing_cookie):
                        session = LoginSession(chooser=chooser, mfa=True, missing_cookie=missing_cookie)
                        manager, created, transport = await self.run_batch([session], flow)
                        job = manager.jobs[created[0]["id"]]
                        self.assertEqual(job.status, "success")
                        self.assertTrue(any(line.endswith(PERSONAL_LOG) for line in job.logs))
                        calls = [url for _, url, _ in session.requests]
                        self.assertEqual(sum("workspace/select" in url for url in calls), 1)
                        self.assertEqual(sum("client_auth_session_dump" in url for url in calls), int(missing_cookie))
                        transport.post.assert_awaited_once()
                        self.assertEqual(transport.post.call_args.kwargs["headers"]["ChatGPT-Account-Id"], "personal-fixture")
                        store = PasskeyHandoffs()
                        token = manager.issue_handoff(job.id, store)
                        self.assertEqual(store.consume(token), URL)
                        with self.assertRaises(KeyError):
                            store.consume(token)

    async def test_mixed_concurrent_batch_keeps_ordinary_login_unchanged(self):
        for flow in ("anti409", "legacy"):
            with self.subTest(flow=flow):
                sessions = [LoginSession(chooser="none", mfa=True), LoginSession(mfa=True), LoginSession(mfa=True, missing_cookie=True)]
                for index, session in enumerate(sessions):
                    account_id = f"private-account-{index}"
                    session.workspaces = [WORKSPACES[0], {"id": account_id, "kind": "personal"}]
                    session.session_payload = {"accessToken": f"private-token-{index}", "account": {"id": account_id}}
                manager, _, transport = await self.run_batch(sessions, flow)
                self.assertTrue(all(job.status == "success" for job in manager.jobs.values()))
                self.assertEqual(transport.post.await_count, 3)
                self.assertEqual(
                    {(call.kwargs["headers"]["Authorization"], call.kwargs["headers"]["ChatGPT-Account-Id"])
                     for call in transport.post.await_args_list},
                    {(f"Bearer private-token-{index}", f"private-account-{index}") for index in range(3)},
                )
                for session in sessions[1:]:
                    selected = [args["json"]["workspace_id"] for _, url, args in session.requests if "workspace/select" in url]
                    self.assertEqual(selected, [session.session_payload["account"]["id"]])
                self.assertFalse(any("workspace/select" in url or "client_auth_session_dump" in url for _, url, _ in sessions[0].requests))
                self.assertEqual(sum(line.endswith(PERSONAL_LOG) for job in manager.jobs.values() for line in job.logs), 2)

    async def test_unavailable_personal_never_prepares_or_issues_handoff(self):
        for options in (
            {"workspaces": WORKSPACES[:1]},
            {"workspaces": [{"id": "disabled", "kind": "personal", "disabled_reason": "blocked"}]},
            {"missing_cookie": True, "dump_status": 500},
            {"session_payload": {"WARNING_BANNER": "fixture"}},
        ):
            with self.subTest(options=options):
                manager, created, transport = await self.run_batch([LoginSession(mfa=True, **options)], "anti409")
                job = manager.jobs[created[0]["id"]]
                self.assertEqual(job.status, "error")
                self.assertIsNone(job.handoff_url)
                transport.post.assert_not_awaited()
                with self.assertRaises(ValueError):
                    manager.issue_handoff(job.id, PasskeyHandoffs())
