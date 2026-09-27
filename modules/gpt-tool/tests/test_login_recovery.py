from types import SimpleNamespace

import pytest

from gpt_tool import login, oauth
from gpt_tool.parser import Credentials


class Cookies:
    def __init__(self, device_id: str = "") -> None:
        self.jar = []
        if device_id:
            self.set("oai-did", device_id, domain="auth.openai.com")

    def set(self, name: str, value: str, domain: str = "") -> None:
        self.jar = [cookie for cookie in self.jar if (cookie.name, cookie.domain) != (name, domain)]
        self.jar.append(SimpleNamespace(name=name, value=value, domain=domain))


class Response:
    def __init__(self, status: int, payload=None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_auth_calls_share_server_device_id(monkeypatch) -> None:
    calls = []

    class Session:
        cookies = Cookies("server-device")

        def post(self, url, *, headers, json):
            calls.append((url, headers.copy()))
            if url == login.URL_AUTHORIZE_CONTINUE:
                return Response(200)
            if url == login.URL_PASSWORD_VERIFY:
                return Response(
                    200,
                    {
                        "page": {"type": "mfa", "payload": {"factor_id": "challenge"}},
                        "continue_url": f"{login.AUTH}/mfa-challenge/challenge",
                    },
                )
            if url == login.URL_MFA_VERIFY:
                return Response(200, {"continue_url": "https://auth.openai.com/done"})
            return Response(200)

    monkeypatch.setattr(login, "get_sentinel_token_pow", lambda *_args: "sentinel")
    monkeypatch.setattr(login, "generate_code", lambda _secret: "123456")

    creds = Credentials(email="user@example.com", password="password", totp_secret="SECRET")
    login.submit_auth_password_mfa(Session(), creds, "initial-device")

    assert [headers["oai-device-id"] for _url, headers in calls] == ["server-device"] * 4


def test_mfa_invalid_state_does_not_retry_dead_challenge(monkeypatch) -> None:
    calls = []

    class Session:
        cookies = Cookies()

        def post(self, url, *, headers, json):
            calls.append(url)
            if url == login.URL_MFA_ISSUE:
                return Response(200)
            return Response(
                409,
                text='{"error":{"message":"Your sign-in session is no longer valid.","code":"invalid_state"}}',
            )

    monkeypatch.setattr(login, "generate_code", lambda _secret: "123456")

    with pytest.raises(login.LoginError, match="invalid_state"):
        login._mfa_verify(Session(), "challenge", "SECRET", "device")

    assert calls == [login.URL_MFA_ISSUE, login.URL_MFA_VERIFY]


def test_login_rebuilds_session_after_invalid_state(monkeypatch) -> None:
    sessions = [SimpleNamespace(cookies=Cookies()), SimpleNamespace(cookies=Cookies())]
    attempts = []

    monkeypatch.setattr(login, "build_client", lambda *_args, **_kwargs: sessions.pop(0))
    monkeypatch.setattr(login.time, "sleep", lambda _seconds: None)

    def fake_oauth(session, _creds, device_id):
        attempts.append((session, device_id))
        if len(attempts) == 1:
            raise login.LoginError("network", "mfa/verify HTTP 409: invalid_state")
        return oauth.CodexTokens("access", "refresh", "id")

    monkeypatch.setattr(oauth, "oauth_codex_with_password", fake_oauth)
    creds = Credentials(email="user@example.com", password="password", totp_secret="SECRET")

    bundle, session = login.login_keep_session(creds)

    assert session is attempts[1][0]
    assert attempts[0][0] is not attempts[1][0]
    assert attempts[0][1] != attempts[1][1]
    assert bundle.access_token == "access"


def test_non_transient_409_is_not_retried(monkeypatch) -> None:
    calls = 0

    def build(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(cookies=Cookies())

    monkeypatch.setattr(login, "build_client", build)
    monkeypatch.setattr(
        oauth,
        "oauth_codex_with_password",
        lambda *_args: (_ for _ in ()).throw(login.LoginError("credential", "password HTTP 409: wrong")),
    )
    creds = Credentials(email="user@example.com", password="wrong", totp_secret="SECRET")

    with pytest.raises(login.LoginError, match="password HTTP 409"):
        login.login_keep_session(creds)

    assert calls == 1
