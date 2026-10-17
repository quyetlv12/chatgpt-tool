"""Pure-request login fixtures; never contact upstream or use real accounts."""
from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from curl_cffi.requests import Cookies

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from session_phase import SessionError, get_session_pure_request, is_fatal_login_error


AUTH = "https://auth.openai.com"
CALLBACK = "https://chatgpt.com/api/auth/callback/openai?code=synthetic&state=fixture"
WORKSPACES = [
    {"id": "edu-fixture", "name": "Fixture EDU", "kind": "organization"},
    {"id": "personal-fixture", "name": None, "kind": "personal"},
]


class Response:
    def __init__(self, url, payload=None, status=200, headers=None):
        self.url, self.status_code = url, status
        self.headers = headers or {}
        self.text = "private-upstream-body"
        self.payload = payload or {}

    def json(self):
        return self.payload


class LoginSession:
    def __init__(self, *, chooser="direct", mfa=False, workspaces=None,
                 select_status=200, selected_url=CALLBACK, malformed_cookie=False,
                 cookie_domain="auth.openai.com", session_payload=None,
                 missing_cookie=False, dump_status=200, dump_payload=None):
        self.cookies = Cookies()
        self.chooser, self.mfa = chooser, mfa
        self.workspaces = WORKSPACES if workspaces is None else workspaces
        self.select_status, self.selected_url = select_status, selected_url
        self.malformed_cookie = malformed_cookie
        self.cookie_domain = cookie_domain
        self.session_payload = session_payload
        self.missing_cookie, self.dump_status = missing_cookie, dump_status
        self.dump_payload = dump_payload
        self.requests = []
        self.closed = False

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        if url == AUTH + "/api/accounts/client_auth_session_dump":
            payload = self.dump_payload
            if payload is None:
                payload = {"client_auth_session": {"workspaces": self.workspaces}} if self.missing_cookie else {}
            return Response(url, payload, status=self.dump_status)
        if "/api/accounts/authorize?" in url:
            if self.chooser in ("reauthorize", "no-continuation") and self._authenticated:
                return Response(url, status=302, headers={"Location": "/workspace"})
            return Response(AUTH + "/log-in/password")
        if url == "https://chatgpt.com/fixture-continue":
            return Response(url, status=302, headers={"Location": AUTH + "/workspace"})
        if url == AUTH + "/fixture-personal-continue":
            return Response(url, status=302, headers={"Location": CALLBACK})
        if url == AUTH + "/fixture-loop":
            return Response(url, status=302, headers={"Location": AUTH + "/workspace"})
        if url == "https://chatgpt.com/api/auth/session":
            if self.session_payload is not None:
                return Response(url, self.session_payload)
            return Response(url, {"accessToken": "synthetic-token", "account": {"id": "personal-fixture"}})
        return Response(url)

    @property
    def _authenticated(self):
        return any("/password/verify" in url for method, url, _ in self.requests if method == "POST")

    def _post_auth_payload(self):
        raw = base64.urlsafe_b64encode(json.dumps({"workspaces": self.workspaces}).encode()).decode().rstrip("=")
        if not self.missing_cookie:
            self.cookies.set("oai-client-auth-session", "broken" if self.malformed_cookie else raw + ".signature",
                             domain=self.cookie_domain, path="/")
        if self.chooser == "none":
            return {"continue_url": CALLBACK, "page": {"type": "external_redirect"}}
        if self.chooser == "redirect":
            return {"continue_url": "https://chatgpt.com/fixture-continue"}
        if self.chooser == "reauthorize":
            return {"continue_url": AUTH + "/login-enroll-passkey"}
        if self.chooser == "no-continuation":
            return {}
        return {"continue_url": "/workspace", "page": {"type": "workspace_selection"}}

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        if "/api/auth/signin/openai?" in url:
            return Response(url, {"url": AUTH + "/api/accounts/authorize?state=fixture"})
        if url == AUTH + "/api/accounts/password/verify":
            if self.mfa:
                return Response(url, {"continue_url": "/mfa-challenge/abcdef", "page": {"type": "mfa_challenge"}})
            return Response(url, self._post_auth_payload())
        if url == AUTH + "/api/accounts/mfa/issue_challenge":
            return Response(url)
        if url == AUTH + "/api/accounts/mfa/verify":
            return Response(url, self._post_auth_payload())
        if url == AUTH + "/api/accounts/workspace/select":
            return Response(url, {"continue_url": self.selected_url}, status=self.select_status)
        raise AssertionError("unexpected synthetic POST")

    def close(self):
        self.closed = True


class PersonalWorkspaceLoginTests(unittest.IsolatedAsyncioTestCase):
    async def login(self, session, flow="anti409"):
        logs, callbacks = [], []

        def consume(client, url, _log):
            callbacks.append(url)
            client.cookies.set("__Secure-next-auth.session-token", "synthetic-session", domain="chatgpt.com", secure=True)
            return True

        with (
            patch("request_phase._create_session", return_value=session),
            patch("request_phase._step_csrf", return_value="fixture-csrf"),
            patch("request_phase._get_sentinel_token", return_value="fixture-sentinel"),
            patch("request_phase._consume_callback", side_effect=consume),
            patch("totp_helper.generate_code", return_value="123456"),
        ):
            result = await get_session_pure_request(
                email="workspace@example.test", password="fixture-password", secret="FIXTURE",
                login_flow=flow, log=logs.append,
            )
        return result, callbacks, logs

    async def test_ordinary_account_does_not_select_or_reauthorize(self):
        for flow in ("anti409", "legacy"):
            for mfa in (False, True):
                with self.subTest(flow=flow, mfa=mfa):
                    session = LoginSession(chooser="none", mfa=mfa)
                    result, callbacks, _ = await self.login(session, flow)
                    self.assertEqual(result["accessToken"], "synthetic-token")
                    self.assertEqual(callbacks, [CALLBACK])
                    self.assertFalse(any("workspace/select" in url for _, url, _ in session.requests))
                    self.assertFalse(any("client_auth_session_dump" in url for _, url, _ in session.requests))
                    self.assertEqual(sum("signin/openai" in url for _, url, _ in session.requests), 1)
                    self.assertTrue(session.closed)

    async def test_selects_personal_not_first_organization_before_callback(self):
        for flow in ("anti409", "legacy"):
            for mfa in (False, True):
                for chooser in ("direct", "redirect", "reauthorize", "no-continuation"):
                    with self.subTest(flow=flow, mfa=mfa, chooser=chooser):
                        session = LoginSession(chooser=chooser, mfa=mfa)
                        result, callbacks, logs = await self.login(session, flow)
                        self.assertEqual(result["account"]["id"], "personal-fixture")
                        self.assertEqual(callbacks, [CALLBACK])
                        selected = [args for method, url, args in session.requests if "workspace/select" in url]
                        self.assertEqual(len(selected), 1)
                        self.assertEqual(selected[0]["json"], {"workspace_id": "personal-fixture"})
                        self.assertEqual(selected[0]["headers"]["Referer"], AUTH + "/workspace")
                        self.assertFalse(selected[0]["allow_redirects"])
                        self.assertNotIn("personal-fixture", "\n".join(logs))
                        self.assertTrue(session.closed)
                        self.assertFalse(any("client_auth_session_dump" in url for _, url, _ in session.requests))

    async def test_missing_cookie_fetches_official_session_dump_and_selects_personal(self):
        for flow in ("anti409", "legacy"):
            for chooser in ("direct", "redirect", "reauthorize", "no-continuation"):
                with self.subTest(flow=flow, chooser=chooser):
                    session = LoginSession(missing_cookie=True, mfa=True, chooser=chooser)
                    result, callbacks, logs = await self.login(session, flow)
                    self.assertEqual(result["account"]["id"], "personal-fixture")
                    self.assertEqual(callbacks, [CALLBACK])
                    dumps = [args for method, url, args in session.requests if "client_auth_session_dump" in url]
                    self.assertEqual(len(dumps), 1)
                    self.assertFalse(dumps[0]["allow_redirects"])
                    self.assertEqual(dumps[0]["headers"]["Referer"], AUTH + "/workspace")
                    self.assertNotIn("personal-fixture", "\n".join(logs))

    async def test_invalid_dump_never_selects_organization_or_completes_login(self):
        for case in (
            {"dump_status": 302}, {"dump_status": 401}, {"dump_status": 500},
            {"dump_payload": {"client_auth_session": None}},
            {"dump_payload": {"workspaces": WORKSPACES}},
            {"workspaces": WORKSPACES[:1]},
            {"workspaces": [{"id": "p", "kind": "personal", "disabled_reason": "blocked"}]},
            {"workspaces": [{"id": "p1", "kind": "personal"}, {"id": "p2", "kind": "personal"}]},
        ):
            with self.subTest(case=case):
                session = LoginSession(missing_cookie=True, **case)
                with self.assertRaises(SessionError) as caught:
                    await self.login(session)
                self.assertNotIn("private-upstream-body", str(caught.exception))
                self.assertFalse(any("workspace/select" in url or "/api/auth/session" in url
                                     for _, url, _ in session.requests))
                self.assertTrue(session.closed)

    async def test_missing_disabled_ambiguous_or_malformed_personal_fails_closed(self):
        cases = [
            {"workspaces": WORKSPACES[:1]},
            {"workspaces": [{"id": "disabled", "kind": "personal", "disabled_reason": "blocked"}]},
            {"workspaces": [{"id": "p1", "kind": "personal"}, {"id": "p2", "kind": "personal"}]},
            {"workspaces": [{"id": "org", "kind": "organization", "name": "Personal account"}]},
            {"workspaces": [{"id": "bad\nheader", "kind": "personal"}]},
            {"workspaces": [{"id": "unknown", "name": "Personal account"}]},
            {"malformed_cookie": True},
            {"cookie_domain": "chatgpt.com"},
        ]
        for case in cases:
            with self.subTest(case=case):
                session = LoginSession(**case)
                with self.assertRaises(SessionError) as caught:
                    await self.login(session)
                self.assertFalse(is_fatal_login_error(caught.exception))
                self.assertFalse(any("workspace/select" in url for _, url, _ in session.requests))
                self.assertFalse(any("/api/auth/session" in url for _, url, _ in session.requests))
                self.assertTrue(session.closed)

    async def test_selected_personal_continuation_follows_existing_state_without_reauthorize(self):
        session = LoginSession(selected_url="/fixture-personal-continue")
        result, callbacks, _ = await self.login(session)
        self.assertEqual(result["accessToken"], "synthetic-token")
        self.assertEqual(callbacks, [CALLBACK])
        self.assertEqual(sum("signin/openai" in url for _, url, _ in session.requests), 1)

    async def test_workspace_failure_in_reauthorization_is_not_swallowed_or_retried(self):
        session = LoginSession(chooser="reauthorize", select_status=409)
        with self.assertRaisesRegex(SessionError, "Chọn Personal account thất bại: HTTP 409"):
            await self.login(session)
        self.assertEqual(sum("workspace/select" in url for _, url, _ in session.requests), 1)
        self.assertFalse(any("/api/auth/session" in url for _, url, _ in session.requests))

    async def test_repeated_workspace_gate_does_not_resubmit_selection(self):
        session = LoginSession(selected_url="/fixture-loop")
        with self.assertRaisesRegex(SessionError, "quay lại màn hình chọn workspace"):
            await self.login(session)
        self.assertEqual(sum("workspace/select" in url for _, url, _ in session.requests), 1)
        self.assertTrue(session.closed)

    async def test_selection_does_not_replace_session_token_verification(self):
        session = LoginSession(session_payload={"WARNING_BANNER": "fixture"})
        with self.assertRaisesRegex(SessionError, "WARNING_BANNER"):
            await self.login(session)
        self.assertTrue(session.closed)

    async def test_selection_failure_or_untrusted_continuation_never_completes_login(self):
        for case in (
            {"select_status": 409}, {"select_status": 500},
            {"select_status": 302}, {"selected_url": ""},
            {"selected_url": "https://example.test/steal"},
            {"selected_url": "https://auth.openai.com.example.test/workspace"},
            {"selected_url": "//example.test/steal"},
            {"selected_url": "https://auth.openai.com:444/continue"},
            {"selected_url": AUTH + "/workspace"},
        ):
            with self.subTest(case=case):
                session = LoginSession(**case)
                with self.assertRaises(SessionError) as caught:
                    await self.login(session)
                self.assertNotIn("private-upstream-body", str(caught.exception))
                self.assertFalse(any("/api/auth/session" in url for _, url, _ in session.requests))
                self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
