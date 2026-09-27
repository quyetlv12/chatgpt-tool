from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from session_phase import get_session_pure_request  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
