from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from session_phase import (  # noqa: E402
    get_session_pure_request,
    is_fatal_login_error,
    is_transient_login_error,
    login_error_http_status,
)


class _Cookies:
    def __init__(self) -> None:
        self.jar: list[SimpleNamespace] = []
        self.set_calls: list[tuple[str, str, str]] = []

    def set(self, name: str, value: str, *, domain: str) -> None:
        self.set_calls.append((name, value, domain))
        self.jar = [
            cookie
            for cookie in self.jar
            if (cookie.name, cookie.domain) != (name, domain)
        ]
        self.jar.append(
            SimpleNamespace(name=name, value=value, domain=domain, path="/")
        )


class _Response:
    def __init__(self, url: str, payload: dict | None = None) -> None:
        self.status_code = 200
        self.url = url
        self._payload = payload or {}
        self.text = ""

    def json(self) -> dict:
        return self._payload


class _Session:
    def __init__(self) -> None:
        self.cookies = _Cookies()

    def get(self, url: str, **_kwargs):
        if "/api/accounts/authorize?" in url:
            self.cookies.set("oai-did", "server-device", domain="auth.openai.com")
            return _Response("https://auth.openai.com/log-in/password")
        return _Response(url)

    def post(self, url: str, **_kwargs):
        if "/api/auth/signin/openai?" in url:
            return _Response(
                url,
                {"url": "https://auth.openai.com/api/accounts/authorize?state=test"},
            )
        raise AssertionError(f"unexpected POST {url}")

    def close(self) -> None:
        return None


class LoginDeviceStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_seeds_auth_host_and_adopts_server_device(self) -> None:
        session = _Session()
        sentinel_device_ids: list[str] = []

        def stop_after_bootstrap(_session, device_id, *_args):
            sentinel_device_ids.append(device_id)
            raise RuntimeError("stop after bootstrap")

        with (
            patch("request_phase._create_session", return_value=session),
            patch("request_phase._step_csrf", return_value="csrf"),
            patch(
                "request_phase._get_sentinel_token",
                side_effect=stop_after_bootstrap,
            ),
            self.assertRaisesRegex(RuntimeError, "stop after bootstrap"),
        ):
            await get_session_pure_request(
                email="user@example.test",
                password="password",
                secret="SECRET",
                log=lambda _line: None,
            )

        device_cookies = {
            cookie.domain: cookie.value
            for cookie in session.cookies.jar
            if cookie.name == "oai-did"
        }
        self.assertTrue(device_cookies.get("chatgpt.com"))
        initial_device = device_cookies["chatgpt.com"]
        self.assertIn(("oai-did", initial_device, "auth.openai.com"), session.cookies.set_calls)
        self.assertEqual(sentinel_device_ids, ["server-device"])


class LoginErrorClassificationTests(unittest.TestCase):
    def test_transient_verification_statuses_are_not_invalid_credentials(self) -> None:
        for status in (302, 403, 404, 408, 409, 429, 500, 502, 503, 504):
            message = f"password verify failed: HTTP {status} - upstream response"
            with self.subTest(status=status):
                self.assertEqual(login_error_http_status(message), status)
                self.assertTrue(is_transient_login_error(message))
                self.assertFalse(is_fatal_login_error(message))

    def test_credential_statuses_remain_fatal(self) -> None:
        for status in (400, 401, 422):
            message = f"password verify failed: HTTP {status} - invalid credentials"
            with self.subTest(status=status):
                self.assertTrue(is_fatal_login_error(message))


if __name__ == "__main__":
    unittest.main()
