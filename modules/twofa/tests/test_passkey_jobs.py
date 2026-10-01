from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "change 2fa community"
for path in (str(ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from passkey_jobs import PasskeyJobManager  # noqa: E402
from passkey_service import PasskeyHandoffs  # noqa: E402
import server  # noqa: E402


URL = "https://auth.openai.com/passkey-enroll?origin_app_name=ChatGPT&mfa_token=synthetic-state"


class PasskeyBulkManagerTests(IsolatedAsyncioTestCase):
    async def test_append_after_handoff_uses_fresh_credentials_and_keeps_previous_job(self):
        service = SimpleNamespace(prepare_passkey=AsyncMock(return_value=URL))
        manager = PasskeyJobManager(service=service, max_concurrent=2)
        first = manager.add(["repeat@example.com|old-password|OLD-TOTP"])[0]
        manager.start()
        try:
            await asyncio.wait_for(manager._queue.join(), 1)
            handoffs = PasskeyHandoffs()
            token = manager.issue_handoff(first["id"], handoffs)
            self.assertFalse(manager.is_busy(" REPEAT@example.com "))
            added = manager.add([
                "REPEAT@example.com|new-password|NEW-TOTP",
                "new@example.com|password|TOTP",
            ])
            self.assertEqual(len(added), 2)
            self.assertNotEqual(added[0]["id"], first["id"])
            self.assertEqual(manager.jobs[first["id"]].status, "success")
            with self.assertRaises(ValueError):
                manager.retry(first["id"])
            await asyncio.wait_for(manager._queue.join(), 1)
            self.assertEqual(len(manager.jobs), 3)
            self.assertTrue(all(j.status == "success" for j in manager.jobs.values()))
            self.assertEqual(handoffs.consume(token), URL)
            calls = service.prepare_passkey.await_args_list
            self.assertTrue(any(c.kwargs["password"] == "new-password" for c in calls))
        finally:
            await manager.shutdown()

    async def test_bulk_login_diagnostics_are_fixed_and_credential_free(self) -> None:
        async def prepare(**kwargs):
            kwargs["log"]("[login] lần 1/3 chưa thành công — thử lại...")
            return URL

        service = SimpleNamespace(prepare_passkey=AsyncMock(side_effect=prepare))
        manager = PasskeyJobManager(service=service, max_concurrent=1)
        created = manager.add(["safe@example.com|synthetic-password|CURRENT-TOTP"])
        manager.start()
        try:
            await asyncio.wait_for(manager._queue.join(), timeout=1)
            logs = manager.logs(created[0]["id"])
            self.assertTrue(any("[auth]" in line for line in logs))
            self.assertFalse(any("lần 1/3" in line for line in logs))
            self.assertNotIn("synthetic-password", json.dumps(manager.snapshots()))
            self.assertNotIn("CURRENT-TOTP", json.dumps(manager.snapshots()))
        finally:
            await manager.shutdown()

    async def test_bulk_workers_prepare_handoff_without_persisting_credentials(self) -> None:
        service = SimpleNamespace(prepare_passkey=AsyncMock(return_value=URL))
        manager = PasskeyJobManager(service=service, max_concurrent=2)
        created = manager.add([
            "first@example.com|synthetic-password|CURRENT-TOTP",
            "second@example.com|synthetic-password|SECOND-TOTP",
        ])
        self.assertEqual(len(created), 2)
        self.assertEqual(
            [(job["window_index"], job["window_total"]) for job in created],
            [(1, 2), (2, 2)],
        )
        self.assertNotIn("synthetic-password", json.dumps(created))
        self.assertNotIn("CURRENT-TOTP", json.dumps(manager.snapshots()))

        manager.start()
        try:
            await asyncio.wait_for(manager._queue.join(), timeout=1)
            self.assertEqual([job.status for job in manager.jobs.values()], ["success", "success"])
            self.assertTrue(all(job.handoff_url == URL for job in manager.jobs.values()))
            handoffs = PasskeyHandoffs()
            token = manager.issue_handoff(created[0]["id"], handoffs)
            self.assertEqual(handoffs.consume(token), URL)
            self.assertTrue(manager.jobs[created[0]["id"]].handoff_issued)
            self.assertNotIn("mfa_token", json.dumps(manager.snapshots()))
        finally:
            await manager.shutdown()

        service.prepare_passkey.assert_awaited()

    async def test_conflict_retry_and_stop_are_scoped_to_bulk_queue(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def pending(**_kwargs):
            entered.set()
            await release.wait()
            return URL

        service = SimpleNamespace(prepare_passkey=AsyncMock(side_effect=pending))
        manager = PasskeyJobManager(service=service, max_concurrent=1)
        first = manager.add(["first@example.com|password|TOTP"])[0]
        manager.start()
        try:
            await entered.wait()
            with self.assertRaises(ValueError):
                manager.add(["first@example.com|password|TOTP"])
            stopped = manager.stop(first["id"])
            self.assertEqual(stopped["status"], "running")
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(manager.jobs[first["id"]].status, "cancelled")
            retried = manager.retry(first["id"])
            self.assertEqual(retried["status"], "queued")
        finally:
            release.set()
            await manager.shutdown()


class PasskeyBulkApiTests(TestCase):
    def setUp(self) -> None:
        self.manager = PasskeyJobManager(service=SimpleNamespace(prepare_passkey=AsyncMock()))
        self.manager.jobs["ready"] = self.manager_job()
        self.manager.order.append("ready")
        self.password_manager = SimpleNamespace(jobs={})
        self.twofa_manager = SimpleNamespace(assert_not_logging_out=lambda _email: None, jobs={})
        self.handoffs = PasskeyHandoffs()
        self.patches = [
            patch.object(server, "passkey_manager", self.manager),
            patch.object(server, "password_manager", self.password_manager),
            patch.object(server, "manager", self.twofa_manager),
            patch.object(server, "passkey_handoffs", self.handoffs),
        ]
        for patcher in self.patches:
            patcher.start()
        self.client = TestClient(server.app)
        self.headers = {"X-Auth-Token": server.auth_token}

    @staticmethod
    def manager_job():
        manager = PasskeyJobManager()
        manager.add(["ready@example.com|password|TOTP"])
        job = manager.jobs[manager.order[0]]
        job.id = "ready"
        job.status = "success"
        job.phase = "handoff_ready"
        job.handoff_url = URL
        return job

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()

    def test_launch_requires_auth_and_confirmation_and_is_one_use(self) -> None:
        response = self.client.post("/api/passkey/jobs/ready/launch", json={"confirm": "LAUNCH_PASSKEY"})
        self.assertEqual(response.status_code, 401)
        response = self.client.post(
            "/api/passkey/jobs/ready/launch",
            headers=self.headers,
            json={"confirm": "LAUNCH_PASSKEY"},
        )
        self.assertEqual(response.status_code, 200)
        launch_path = response.json()["launch_path"]
        self.assertTrue(launch_path.startswith("/api/passkey/launch/"))
        self.assertEqual(self.manager.jobs["ready"].snapshot()["handoff_issued"], True)
        self.assertEqual(
            self.client.get(launch_path, follow_redirects=False).status_code,
            303,
        )
        self.assertEqual(
            self.client.get(launch_path).status_code,
            410,
        )
        self.assertEqual(
            self.client.post("/api/passkey/jobs/ready/launch", headers=self.headers, json={"confirm": "LAUNCH_PASSKEY"}).status_code,
            409,
        )

    def test_batch_route_does_not_echo_credentials(self) -> None:
        response = self.client.post(
            "/api/passkey/jobs",
            headers=self.headers,
            json={"lines": ["bulk@example.com|synthetic-password|CURRENT-TOTP"]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("synthetic-password", response.text)
        self.assertNotIn("CURRENT-TOTP", response.text)

    def test_native_launch_uses_configured_suite_host_and_keeps_one_use_redirect(self):
        with patch.object(server, "RUNTIME_HOST", "twofa.localhost"), \
             patch.object(server, "RUNTIME_PORT", 5050), \
             patch.object(server.passkey_windows, "open", return_value=True) as launch:
            response = self.client.post("/api/passkey/jobs/ready/launch", headers=self.headers,
                                        json={"confirm": "LAUNCH_PASSKEY", "native_window": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["opened"])
        launch.assert_called_once_with(
            "http://twofa.localhost:5050" + response.json()["launch_path"],
            index=1,
            total=1,
        )
        self.assertNotIn("mfa_token", response.text)
        self.assertEqual(self.client.get(response.json()["launch_path"], follow_redirects=False).status_code, 303)

    def test_native_failure_preserves_manual_fallback(self):
        with patch.object(server.passkey_windows, "open", return_value=False):
            response = self.client.post("/api/passkey/jobs/ready/launch", headers=self.headers,
                                        json={"confirm": "LAUNCH_PASSKEY", "native_window": True})
        self.assertFalse(response.json()["opened"])
        self.assertEqual(self.client.get(response.json()["launch_path"], follow_redirects=False).status_code, 303)

    def test_close_passkey_windows_only_calls_owned_window_launcher(self):
        with patch.object(server.passkey_windows, "close_all", return_value=2) as close_all:
            response = self.client.post("/api/passkey/windows/close", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"closed": 2})
        close_all.assert_called_once_with()

    def test_close_passkey_windows_requires_authentication(self):
        response = self.client.post("/api/passkey/windows/close")
        self.assertEqual(response.status_code, 401)

    def test_completed_account_can_be_submitted_again_with_new_account(self):
        self.manager.issue_handoff("ready", self.handoffs)
        response = self.client.post("/api/passkey/jobs", headers=self.headers, json={
            "lines": ["READY@example.com|updated-password|NEW-TOTP", "new@example.com|pw|TOTP"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["jobs"]), 2)
        self.assertEqual(len(self.manager.jobs), 3)
        self.assertNotIn("updated-password", response.text)

    def test_active_duplicate_rejects_batch_without_partial_enqueue(self):
        self.manager.jobs["ready"].status = "running"
        response = self.client.post("/api/passkey/jobs", headers=self.headers, json={
            "lines": ["new@example.com|pw|TOTP", "READY@example.com|pw|TOTP"],
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.manager.jobs), 1)

    def test_new_account_can_be_appended_while_another_is_running(self):
        self.manager.jobs["ready"].status = "running"
        response = self.client.post("/api/passkey/jobs", headers=self.headers, json={
            "lines": ["new@example.com|pw|TOTP"],
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.manager.jobs["ready"].status, "running")
