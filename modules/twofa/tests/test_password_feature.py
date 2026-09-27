from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "change 2fa community"
for path in (str(ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from db.engine import DatabaseEngine  # noqa: E402
from db.repositories import JobRepository, RepositoryError, SettingsRepository  # noqa: E402
from password_jobs import PasswordJobManager  # noqa: E402
from password_phase import (  # noqa: E402
    PasswordMutationRejected,
    PasswordMutationUncertain,
    _change_password_sync,
)
from password_service import (  # noqa: E402
    PasswordChangeError,
    PasswordChangeResult,
    PasswordService,
)
import server  # noqa: E402


class PasswordSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = DatabaseEngine(Path(self.temp_dir.name) / "settings.db")
        self.repo = SettingsRepository(self.engine)

    def tearDown(self) -> None:
        self.engine.close()
        self.temp_dir.cleanup()

    def test_target_password_is_validated_and_audit_redacted(self) -> None:
        target = "Safe-target-password-2026!"
        self.repo.set("password_change.target_password", target)

        self.assertEqual(self.repo.get("password_change.target_password"), target)
        row = self.engine.raw_connection().execute(
            """SELECT payload_json FROM icloud_audit_log
               WHERE event_type = 'settings.set'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        self.assertEqual(json.loads(row["payload_json"])["new_value"], "***")

    def test_target_password_rejects_unsafe_values(self) -> None:
        invalid = (
            "short",
            "twelve-chars|bad",
            "valid-length-but-newline\n",
            "valid-length-but-nul\x00",
            "x" * 129,
        )
        for value in invalid:
            with self.subTest(value=repr(value)):
                with self.assertRaises(RepositoryError):
                    self.repo.set("password_change.target_password", value)


class PasswordRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = DatabaseEngine(Path(self.temp_dir.name) / "password.db")
        self.repo = JobRepository(self.engine)

    def tearDown(self) -> None:
        self.engine.close()
        self.temp_dir.cleanup()

    def test_verified_success_promotes_pending_password_atomically(self) -> None:
        self.repo.create({
            "id": "password-job",
            "email": "demo@example.com",
            "combo": "[redacted]",
            "mail_mode": "none",
            "status": "running",
            "password": "old-password",
            "pending_password": "new-password-2026!",
            "secret": "CURRENT-TOTP",
            "account_check": json.dumps({"mutation_started": True}),
            "created_at": 1.0,
            "job_type": "password_community",
        })

        self.repo.complete_password_success(
            job_id="password-job",
            account_check=json.dumps({"phase": "verified", "login_verified": True}),
            changed_at=123.5,
        )

        row = next(item for item in self.repo.list_all() if item["id"] == "password-job")
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["password"], "new-password-2026!")
        self.assertIsNone(row["pending_password"])
        self.assertEqual(
            self.repo.list_password_history(),
            [{
                "id": 1,
                "job_id": "password-job",
                "email": "demo@example.com",
                "password": "new-password-2026!",
                "secret": "CURRENT-TOTP",
                "changed_at": 123.5,
            }],
        )

    def test_existing_v14_jobs_schema_migrates_to_password_checkpoint(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy-v14.db"
        connection = sqlite3.connect(legacy_path)
        connection.execute(
            "CREATE TABLE _schema_version (version INTEGER PRIMARY KEY, description TEXT)"
        )
        connection.execute(
            "INSERT INTO _schema_version (version, description) VALUES (14, 'legacy')"
        )
        connection.execute(
            """CREATE TABLE jobs (
                id TEXT PRIMARY KEY, email TEXT NOT NULL, combo TEXT NOT NULL,
                mail_mode TEXT NOT NULL DEFAULT 'outlook', status TEXT NOT NULL,
                error TEXT, password TEXT, secret TEXT, first_code TEXT,
                user_id TEXT, session_path TEXT, payment_link TEXT,
                session_data TEXT, account_check TEXT, region TEXT,
                mail_account_id TEXT, created_at REAL NOT NULL,
                started_at REAL, finished_at REAL,
                job_type TEXT NOT NULL DEFAULT 'signup'
            )"""
        )
        connection.commit()
        connection.close()

        migrated = DatabaseEngine(legacy_path)
        try:
            columns = {
                row[1]
                for row in migrated.raw_connection().execute("PRAGMA table_info(jobs)")
            }
            history = migrated.raw_connection().execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='password_history'"
            ).fetchone()
            self.assertIn("pending_password", columns)
            self.assertIsNotNone(history)
        finally:
            migrated.close()


class _SettingsRepo:
    def __init__(self, target: str = "First-target-password!") -> None:
        self.target = target

    def get(self, key: str):
        if key == "password_change.target_password":
            return self.target
        return None


class _MemoryJobRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.running_failures = 0
        self.success_failures = 0

    def list_all(self):
        return []

    def create(self, row: dict) -> str:
        self.rows[row["id"]] = dict(row)
        return row["id"]

    def update_status(self, job_id: str, status: str, **kwargs) -> None:
        if status == "running" and self.running_failures:
            self.running_failures -= 1
            raise RuntimeError("simulated password running persistence failure")
        if status == "success" and self.success_failures:
            self.success_failures -= 1
            raise RuntimeError("simulated password success persistence failure")
        self.rows[job_id]["status"] = status
        self.rows[job_id].update(kwargs)

    def append_log(self, _job_id: str, _line: str) -> None:
        return None

    def get_logs(self, _job_id: str):
        return []


class PasswordManagerSecurityTests(unittest.TestCase):
    def test_enqueue_snapshots_target_once_and_never_exposes_credentials(self) -> None:
        settings = _SettingsRepo()
        manager = PasswordJobManager(_MemoryJobRepo(), settings)

        first = manager.add(["first@example.com|old-password|CURRENT-TOTP"])[0]
        first_job = manager.jobs[first["id"]]
        settings.target = "Second-target-password!"
        second = manager.add(["second@example.com|old-password|CURRENT-TOTP"])[0]

        self.assertEqual(first_job.pending_password, "First-target-password!")
        self.assertEqual(
            manager.jobs[second["id"]].pending_password,
            "Second-target-password!",
        )
        encoded = json.dumps(manager.snapshots())
        for secret in (
            "old-password",
            "CURRENT-TOTP",
            "First-target-password!",
            "Second-target-password!",
        ):
            self.assertNotIn(secret, encoded)
        self.assertNotIn("pending_password", first)
        self.assertNotIn("password", first)
        self.assertNotIn("secret", first)


class _SuccessfulPasswordService:
    async def change(self, **kwargs):
        if not kwargs["mutation_started"]:
            await kwargs["checkpoint"]("mutation_started")
        kwargs["log"]("verified without exposing credentials")
        return PasswordChangeResult(login_verified=True)


class _UnverifiedPasswordService:
    async def change(self, **kwargs):
        if not kwargs["mutation_started"]:
            await kwargs["checkpoint"]("mutation_started")
        return PasswordChangeResult(login_verified=False)


class PasswordManagerWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.manager = PasswordJobManager(
            _MemoryJobRepo(),
            _SettingsRepo(),
            service=_SuccessfulPasswordService(),
        )
        self.manager.start()

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()

    async def test_independent_worker_promotes_only_after_verified_result(self) -> None:
        snapshot = self.manager.add([
            "worker@example.com|old-password|CURRENT-TOTP"
        ])[0]
        job = self.manager.jobs[snapshot["id"]]
        for _ in range(100):
            if job.status == "success":
                break
            await asyncio.sleep(0.01)

        self.assertEqual(job.status, "success")
        self.assertTrue(job.login_verified)
        self.assertIsNone(job.pending_password)
        self.assertEqual(
            self.manager.output(),
            ["worker@example.com|First-target-password!|CURRENT-TOTP"],
        )
        serialized = json.dumps(job.snapshot())
        self.assertNotIn("First-target-password!", serialized)
        self.assertNotIn("CURRENT-TOTP", serialized)

    async def test_pre_run_persistence_failure_is_contained(self) -> None:
        self.manager.job_repo.running_failures = 1
        created = self.manager.add([
            "failed@example.com|old-password|CURRENT-TOTP",
            "healthy@example.com|old-password|CURRENT-TOTP",
        ])

        for _ in range(100):
            if all(
                self.manager.jobs[item["id"]].status in {"success", "error"}
                for item in created
            ):
                break
            await asyncio.sleep(0.01)

        failed, healthy = (self.manager.jobs[item["id"]] for item in created)
        self.assertEqual(failed.status, "error")
        self.assertEqual(failed.error_kind, "technical_error")
        self.assertEqual(healthy.status, "success")
        health = self.manager.worker_health()
        self.assertEqual(health["active"], 3)
        self.assertEqual(health["persistence_failures"], 1)

    async def test_success_persistence_failure_becomes_visible_error(self) -> None:
        self.manager.job_repo.success_failures = 1
        created = self.manager.add([
            "failed@example.com|old-password|CURRENT-TOTP",
            "healthy@example.com|old-password|CURRENT-TOTP",
        ])

        for _ in range(100):
            if all(
                self.manager.jobs[item["id"]].status in {"success", "error"}
                for item in created
            ):
                break
            await asyncio.sleep(0.01)

        failed, healthy = (self.manager.jobs[item["id"]] for item in created)
        self.assertEqual(failed.status, "error")
        self.assertIn("không lưu được trạng thái", failed.error.lower())
        self.assertEqual(healthy.status, "success")
        self.assertEqual(self.manager.worker_health()["active"], 3)

    async def test_unverified_result_never_becomes_success_or_output(self) -> None:
        self.manager.service = _UnverifiedPasswordService()
        snapshot = self.manager.add([
            "unverified@example.com|old-password|CURRENT-TOTP"
        ])[0]
        job = self.manager.jobs[snapshot["id"]]

        for _ in range(100):
            if job.status == "error":
                break
            await asyncio.sleep(0.01)

        self.assertEqual(job.status, "error")
        self.assertEqual(job.error_kind, "password_uncertain")
        self.assertTrue(job.mutation_started)
        self.assertIsNotNone(job.pending_password)
        self.assertEqual(self.manager.output(), [])


class PasswordServiceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_logs_are_whitelisted_without_oauth_or_challenge_data(self) -> None:
        captured: list[str] = []
        sensitive_fragments = (
            "demo@example.com",
            "oauth-state-value",
            "challenge-id-value",
            "client-id-value",
        )

        async def login_fn(**kwargs):
            for line in (
                "[session-req] auth URL: https://auth.openai.com/api/accounts/authorize?state=oauth-state-value&client_id=client-id-value",
                "[session-req] password login flow...",
                "[session-req] password verified",
                "[session-req] post-password continue_url=https://auth.openai.com/mfa-challenge/challenge-id-value",
                "[session-req] MFA challenge detected...",
                "[session-req] MFA verified!",
                "[session-req] ✓ done — user: demo@example.com",
            ):
                kwargs["log"](line)
            return {"accessToken": "token", "__cookies": []}

        async def mutate_fn(**_kwargs):
            return None

        service = PasswordService(login_fn=login_fn, mutate_fn=mutate_fn)
        result = await service.change(
            email="demo@example.com",
            current_password="old-password",
            target_password="new-password-2026!",
            secret="CURRENT-TOTP",
            timeout=30,
            mutation_started=False,
            checkpoint=lambda _phase: _noop_checkpoint(),
            log=captured.append,
        )

        self.assertTrue(result.login_verified)
        joined = "\n".join(captured)
        for fragment in sensitive_fragments:
            self.assertNotIn(fragment, joined)
        self.assertIn("[login] Mật khẩu đăng nhập đã được xác minh", joined)
        self.assertIn("[login] Mã TOTP đã được xác minh", joined)
        self.assertIn("[login] Đăng nhập pure-request thành công", joined)

    async def test_success_requires_fresh_login_with_target_password(self) -> None:
        login_passwords: list[str] = []
        mutation_calls: list[tuple[str, str]] = []
        checkpoints: list[str] = []

        async def login_fn(**kwargs):
            login_passwords.append(kwargs["password"])
            return {"accessToken": "token", "__cookies": []}

        async def mutate_fn(**kwargs):
            mutation_calls.append((kwargs["current_password"], kwargs["new_password"]))

        service = PasswordService(login_fn=login_fn, mutate_fn=mutate_fn)
        result = await service.change(
            email="demo@example.com",
            current_password="old-password",
            target_password="new-password-2026!",
            secret="CURRENT-TOTP",
            timeout=30,
            mutation_started=False,
            checkpoint=lambda phase: _record_checkpoint(checkpoints, phase),
            log=lambda _line: None,
        )

        self.assertTrue(result.login_verified)
        self.assertEqual(login_passwords, ["old-password", "new-password-2026!"])
        self.assertEqual(mutation_calls, [("old-password", "new-password-2026!")])
        self.assertEqual(checkpoints, ["mutation_started"])

    async def test_ambiguous_mutation_is_not_resent_and_old_login_marks_not_applied(self) -> None:
        login_passwords: list[str] = []
        mutation_calls = 0

        async def login_fn(**kwargs):
            login_passwords.append(kwargs["password"])
            if kwargs["password"] == "new-password-2026!":
                raise RuntimeError("invalid credentials")
            return {"accessToken": "token", "__cookies": []}

        async def mutate_fn(**_kwargs):
            nonlocal mutation_calls
            mutation_calls += 1
            raise PasswordMutationUncertain("transport outcome unknown")

        service = PasswordService(login_fn=login_fn, mutate_fn=mutate_fn)
        with self.assertRaises(PasswordChangeError) as raised:
            await service.change(
                email="demo@example.com",
                current_password="old-password",
                target_password="new-password-2026!",
                secret="CURRENT-TOTP",
                timeout=30,
                mutation_started=False,
                checkpoint=lambda _phase: _noop_checkpoint(),
                log=lambda _line: None,
            )

        self.assertEqual(raised.exception.error_kind, "password_not_applied")
        self.assertEqual(mutation_calls, 1)
        self.assertEqual(
            login_passwords,
            ["old-password", "new-password-2026!", "old-password"],
        )

    async def test_recovery_checks_target_first_without_resending_mutation(self) -> None:
        login_passwords: list[str] = []

        async def login_fn(**kwargs):
            login_passwords.append(kwargs["password"])
            return {"accessToken": "token", "__cookies": []}

        async def mutate_fn(**_kwargs):
            raise AssertionError("recovery must not resend password mutation")

        service = PasswordService(login_fn=login_fn, mutate_fn=mutate_fn)
        result = await service.change(
            email="demo@example.com",
            current_password="old-password",
            target_password="new-password-2026!",
            secret="CURRENT-TOTP",
            timeout=30,
            mutation_started=True,
            checkpoint=lambda _phase: _noop_checkpoint(),
            log=lambda _line: None,
        )

        self.assertTrue(result.login_verified)
        self.assertEqual(login_passwords, ["new-password-2026!"])


class _FakeCookies:
    def __init__(self) -> None:
        self.values = {"oai-did": "device-id"}

    def set(self, name, value, **_kwargs) -> None:
        self.values[name] = value

    def get(self, name):
        return self.values.get(name)


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict | None = None,
        *,
        url: str = "https://auth.openai.com/",
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.url = url
        self.history: list[_FakeResponse] = []
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.text = ""

    def json(self):
        return self._payload


class _FakeHttpSession:
    def __init__(
        self,
        reset_status: int = 200,
        reset_payload: dict | None = None,
        verified_payload: dict | None = None,
    ) -> None:
        self.cookies = _FakeCookies()
        self.posts: list[tuple[str, dict]] = []
        self.signin_posts: list[tuple[str, dict]] = []
        self.events: list[str] = []
        self.reset_status = reset_status
        self.reset_payload = reset_payload or {
            "page": {"type": "reset_password_success"}
        }
        self.verified_payload = verified_payload or {
            "page": {"type": "reset_password_new_password"},
            "continue_url": "/reset-password/new-password",
        }

    def get(self, url, **_kwargs):
        if url.endswith("/api/auth/csrf"):
            return _FakeResponse(
                200,
                {"csrfToken": "csrf-token"},
                url="https://chatgpt.com/api/auth/csrf",
            )
        if "/authorize?" in url:
            self.events.append("oauth_authorize")
            return _FakeResponse(200, url="https://auth.openai.com/log-in/password")
        return _FakeResponse(200, url=url)

    def post(self, url, **kwargs):
        if "/api/auth/signin/openai?" in url:
            self.events.append("reauth_intent")
            self.signin_posts.append((url, kwargs.get("data") or {}))
            return _FakeResponse(200, {
                "url": "https://auth.openai.com/authorize?state=oauth-state",
            })
        self.posts.append((url, kwargs.get("json") or {}))
        if url.endswith("/password/verify"):
            self.events.append("password_verify")
            return _FakeResponse(200, self.verified_payload)
        if url.endswith("/password/reset"):
            self.events.append("password_reset")
            return _FakeResponse(
                self.reset_status,
                self.reset_payload,
            )
        raise AssertionError(f"unexpected POST {url}")

    def close(self) -> None:
        return None


class PasswordHttpAdapterTests(unittest.TestCase):
    def test_reauth_intent_uses_official_parameters_before_password_verify(self) -> None:
        session = _FakeHttpSession()
        with (
            patch("request_phase._create_session", return_value=session),
            patch("request_phase._get_sentinel_token", return_value="sentinel"),
        ):
            _change_password_sync(
                session_data={
                    "user": {"email": "demo@example.com"},
                    "__cookies": [],
                },
                current_password="old-password",
                new_password="new-password-2026!",
                secret="CURRENT-TOTP",
                log=lambda _line: None,
                _submit_reset=False,
            )

        self.assertEqual(len(session.signin_posts), 1)
        signin_url, form = session.signin_posts[0]
        query = parse_qs(urlparse(signin_url).query)
        self.assertEqual(query["reauth"], ["password"])
        self.assertEqual(query["max_age"], ["0"])
        self.assertEqual(query["post_login_password_reset"], ["true"])
        self.assertEqual(query["login_hint"], ["demo@example.com"])
        self.assertEqual(form["callbackUrl"], "https://chatgpt.com/")
        self.assertLess(
            session.events.index("reauth_intent"),
            session.events.index("password_verify"),
        )

    def test_adapter_refuses_reset_when_reauth_does_not_reach_reset_state(self) -> None:
        session = _FakeHttpSession(verified_payload={
            "page": {"type": "external_url"},
            "continue_url": "https://chatgpt.com/",
        })
        with (
            patch("request_phase._create_session", return_value=session),
            patch("request_phase._get_sentinel_token", return_value="sentinel"),
            self.assertRaises(PasswordMutationRejected),
        ):
            _change_password_sync(
                session_data={"__cookies": []},
                current_password="old-password",
                new_password="new-password-2026!",
                secret="CURRENT-TOTP",
                log=lambda _line: None,
            )

        self.assertNotIn("password_reset", session.events)

    def test_preflight_logs_only_safe_state_and_never_submits_reset(self) -> None:
        session = _FakeHttpSession()
        logs: list[str] = []
        with (
            patch("request_phase._create_session", return_value=session),
            patch("request_phase._get_sentinel_token", return_value="sentinel"),
        ):
            _change_password_sync(
                session_data={"__cookies": [{
                    "name": "auth-session",
                    "value": "cookie-value",
                    "domain": ".openai.com",
                    "path": "/",
                }]},
                current_password="old-password",
                new_password="new-password-2026!",
                secret="CURRENT-TOTP",
                log=logs.append,
                _submit_reset=False,
            )

        self.assertEqual(
            session.posts,
            [("https://auth.openai.com/api/accounts/password/verify", {"password": "old-password"})],
        )
        joined_logs = "\n".join(logs)
        self.assertIn(
            "[password-diag] password_verified "
            "page_type=reset_password_new_password "
            "continue_path=/reset-password/new-password",
            joined_logs,
        )
        self.assertIn(
            "[password-diag] continue_landing "
            "status=200 path=/reset-password/new-password redirects=0",
            joined_logs,
        )
        self.assertIn("[password-diag] preflight complete; reset not submitted", joined_logs)
        self.assertNotIn("old-password", joined_logs)
        self.assertNotIn("new-password-2026!", joined_logs)
        self.assertNotIn("CURRENT-TOTP", joined_logs)

    def test_adapter_uses_official_auth_web_contract_without_browser(self) -> None:
        session = _FakeHttpSession()
        logs: list[str] = []
        with (
            patch("request_phase._create_session", return_value=session),
            patch("request_phase._get_sentinel_token", return_value="sentinel"),
        ):
            _change_password_sync(
                session_data={"__cookies": [{
                    "name": "auth-session",
                    "value": "cookie-value",
                    "domain": ".openai.com",
                    "path": "/",
                }]},
                current_password="old-password",
                new_password="new-password-2026!",
                secret="CURRENT-TOTP",
                log=logs.append,
            )

        self.assertEqual(
            session.posts,
            [
                ("https://auth.openai.com/api/accounts/password/verify", {"password": "old-password"}),
                ("https://auth.openai.com/api/accounts/password/reset", {"password": "new-password-2026!"}),
            ],
        )
        joined_logs = "\n".join(logs)
        self.assertNotIn("old-password", joined_logs)
        self.assertNotIn("new-password-2026!", joined_logs)
        self.assertNotIn("CURRENT-TOTP", joined_logs)

    def test_reset_server_error_is_uncertain_not_definitive_rejection(self) -> None:
        session = _FakeHttpSession(reset_status=500)
        with (
            patch("request_phase._create_session", return_value=session),
            patch("request_phase._get_sentinel_token", return_value="sentinel"),
            self.assertRaises(PasswordMutationUncertain),
        ):
            _change_password_sync(
                session_data={"__cookies": [{
                    "name": "auth-session",
                    "value": "cookie-value",
                    "domain": ".openai.com",
                    "path": "/",
                }]},
                current_password="old-password",
                new_password="new-password-2026!",
                secret="CURRENT-TOTP",
                log=lambda _line: None,
            )

    def test_reset_client_rejection_is_definitive(self) -> None:
        session = _FakeHttpSession(reset_status=400)
        with (
            patch("request_phase._create_session", return_value=session),
            patch("request_phase._get_sentinel_token", return_value="sentinel"),
            self.assertRaises(PasswordMutationRejected),
        ):
            _change_password_sync(
                session_data={"__cookies": [{
                    "name": "auth-session",
                    "value": "cookie-value",
                    "domain": ".openai.com",
                    "path": "/",
                }]},
                current_password="old-password",
                new_password="new-password-2026!",
                secret="CURRENT-TOTP",
                log=lambda _line: None,
            )

    def test_reset_policy_code_is_translated_without_echoing_server_detail(self) -> None:
        unsafe_detail = "rejected supplied credential value"
        session = _FakeHttpSession(
            reset_status=400,
            reset_payload={
                "error": {
                    "code": "password_too_weak",
                    "message": unsafe_detail,
                }
            },
        )
        with (
            patch("request_phase._create_session", return_value=session),
            patch("request_phase._get_sentinel_token", return_value="sentinel"),
            self.assertRaises(PasswordMutationRejected) as raised,
        ):
            _change_password_sync(
                session_data={"__cookies": [{
                    "name": "auth-session",
                    "value": "cookie-value",
                    "domain": ".openai.com",
                    "path": "/",
                }]},
                current_password="old-password",
                new_password="new-password-2026!",
                secret="CURRENT-TOTP",
                log=lambda _line: None,
            )

        self.assertIn("quá yếu", str(raised.exception))
        self.assertNotIn(unsafe_detail, str(raised.exception))

    def test_unknown_reset_code_is_safely_exposed_without_server_detail(self) -> None:
        unsafe_detail = "raw server text must never be surfaced"
        session = _FakeHttpSession(
            reset_status=400,
            reset_payload={
                "error": {
                    "code": "policy_variant_2026",
                    "message": unsafe_detail,
                }
            },
        )
        with (
            patch("request_phase._create_session", return_value=session),
            patch("request_phase._get_sentinel_token", return_value="sentinel"),
            self.assertRaises(PasswordMutationRejected) as raised,
        ):
            _change_password_sync(
                session_data={"__cookies": [{
                    "name": "auth-session",
                    "value": "cookie-value",
                    "domain": ".openai.com",
                    "path": "/",
                }]},
                current_password="old-password",
                new_password="new-password-2026!",
                secret="CURRENT-TOTP",
                log=lambda _line: None,
            )

        self.assertIn("policy_variant_2026", str(raised.exception))
        self.assertNotIn(unsafe_detail, str(raised.exception))


async def _record_checkpoint(values: list[str], phase: str) -> None:
    values.append(phase)


async def _noop_checkpoint() -> None:
    return None


class _ApiSettingsRepo:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_error: str | None = None

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        if self.set_error:
            raise RuntimeError(self.set_error)
        self.values[key] = value

    def delete(self, key: str) -> bool:
        return self.values.pop(key, None) is not None


class _ApiPasswordManager:
    def __init__(self) -> None:
        self.added: list[str] = []

    def snapshots(self):
        return [{"id": "safe-id", "email": "demo@example.com", "status": "queued"}]

    def worker_health(self):
        return {"started": True, "configured": 3, "active": 3, "busy": 0, "queued": 1, "degraded": False}

    def add(self, lines):
        self.added.extend(lines)
        return self.snapshots()


class PasswordApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_settings = server.settings_repo
        self.original_manager = server.password_manager
        self.settings = _ApiSettingsRepo()
        self.manager = _ApiPasswordManager()
        server.settings_repo = self.settings
        server.password_manager = self.manager
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        server.settings_repo = self.original_settings
        server.password_manager = self.original_manager

    def test_password_namespace_requires_local_token(self) -> None:
        self.assertEqual(self.client.get("/api/password-settings").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/api/password/jobs",
                json={"lines": ["demo@example.com|old|SECRET"]},
            ).status_code,
            401,
        )

    def test_target_password_is_revealed_only_for_explicit_local_views(self) -> None:
        target = "Api-target-password-2026!"
        headers = {"X-Auth-Token": server.auth_token}

        updated = self.client.put(
            "/api/password-settings",
            headers=headers,
            json={"target_password": target},
        )
        status = self.client.get("/api/password-settings", headers=headers)
        revealed = self.client.get(
            "/api/password-settings?reveal=true",
            headers=headers,
        )
        bootstrap = self.client.get("/api/password/bootstrap", headers=headers)

        self.assertEqual(updated.json(), {"configured": True})
        self.assertEqual(status.json(), {"configured": True})
        self.assertEqual(
            revealed.json(),
            {"configured": True, "target_password": target},
        )
        self.assertEqual(bootstrap.json()["target_password"], target)
        self.assertNotIn(target, updated.text + status.text)
        for response in (revealed, bootstrap):
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_unconfigured_explicit_views_return_an_empty_password(self) -> None:
        headers = {"X-Auth-Token": server.auth_token}

        revealed = self.client.get(
            "/api/password-settings?reveal=true",
            headers=headers,
        )
        bootstrap = self.client.get("/api/password/bootstrap", headers=headers)

        self.assertEqual(
            revealed.json(),
            {"configured": False, "target_password": ""},
        )
        self.assertEqual(bootstrap.json()["target_password"], "")

    def test_password_jobs_use_separate_api_contract(self) -> None:
        headers = {"X-Auth-Token": server.auth_token}
        response = self.client.post(
            "/api/password/jobs",
            headers=headers,
            json={"lines": ["demo@example.com|old|CURRENT-TOTP"]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["jobs"][0]["id"], "safe-id")
        self.assertEqual(self.manager.added, ["demo@example.com|old|CURRENT-TOTP"])

    def test_target_password_has_explicit_clear_action(self) -> None:
        headers = {"X-Auth-Token": server.auth_token}
        self.settings.values["password_change.target_password"] = "Configured-password!"

        response = self.client.delete("/api/password-settings", headers=headers)

        self.assertEqual(response.json(), {"configured": False})
        self.assertIsNone(self.settings.get("password_change.target_password"))

    def test_validation_response_does_not_echo_secret_input(self) -> None:
        headers = {"X-Auth-Token": server.auth_token}
        response = self.client.put(
            "/api/password-settings",
            headers=headers,
            json={"target_password": "too-short"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertNotIn("too-short", response.text)

    def test_repository_error_does_not_echo_target_password(self) -> None:
        target = "Api-target-password-2026!"
        self.settings.set_error = f"database rejected value {target}"
        response = self.client.put(
            "/api/password-settings",
            headers={"X-Auth-Token": server.auth_token},
            json={"target_password": target},
        )

        self.assertEqual(response.status_code, 503)
        self.assertNotIn(target, response.text)

    def test_health_includes_independent_password_worker_pool(self) -> None:
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["password_worker_health"]["active"], 3)


if __name__ == "__main__":
    unittest.main()
