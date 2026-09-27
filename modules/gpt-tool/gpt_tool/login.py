"""ChatGPT / auth.openai.com password login (Chrome149). Keep cookie jar for Codex OAuth."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from gpt_tool.http_client import (
    LOGIN_UA,
    auth_api_headers,
    build_client,
    chrome_nav_headers,
    chrome_xhr_headers,
)
from gpt_tool.parser import Credentials
from gpt_tool.redaction import redact, short
from gpt_tool.sentinel_pow import get_sentinel_token_pow
from gpt_tool.totp import generate_code

CHATGPT = "https://chatgpt.com"
AUTH = "https://auth.openai.com"
URL_AUTH_LOGIN = f"{CHATGPT}/auth/login"
URL_CSRF = f"{CHATGPT}/api/auth/csrf"
URL_SIGNIN = f"{CHATGPT}/api/auth/signin/openai"
URL_SESSION = f"{CHATGPT}/api/auth/session"
URL_AUTHORIZE_CONTINUE = f"{AUTH}/api/accounts/authorize/continue"
URL_PASSWORD_VERIFY = f"{AUTH}/api/accounts/password/verify"
URL_MFA_ISSUE = f"{AUTH}/api/accounts/mfa/issue_challenge"
URL_MFA_VERIFY = f"{AUTH}/api/accounts/mfa/verify"
COOKIE_SESSION = "__Secure-next-auth.session-token"
_TRANSIENT_STATE_MARKERS = (
    "invalid_state",
    "sign-in session is no longer valid",
)


class LoginError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class SessionBundle:
    email: str
    access_token: str
    session_token: str
    device_id: str
    cookies: dict[str, str]


def _absolutize(current: str, loc: str) -> str:
    if loc.startswith("http://") or loc.startswith("https://"):
        return loc
    return urljoin(current, loc)


def _has_oauth_code(url: str) -> bool:
    q = parse_qs(urlparse(url).query)
    return bool(q.get("code", [""])[0])


def _iter_cookies(session):
    jar = getattr(session.cookies, "jar", None)
    if jar is not None:
        for cookie in jar:
            yield cookie.name, cookie.value
        return
    try:
        for name, value in session.cookies.items():
            yield name, value
    except Exception:
        return


def _session_token_from_jar(session) -> str:
    chunks: dict[int, str] = {}
    base = ""
    for name, value in _iter_cookies(session):
        if name == COOKIE_SESSION:
            base = value
        elif name.startswith(COOKIE_SESSION + "."):
            try:
                idx = int(name.rsplit(".", 1)[1])
            except ValueError:
                continue
            chunks[idx] = value
    if chunks:
        return "".join(chunks[i] for i in sorted(chunks))
    return base


def _device_id(session, fallback: str) -> str:
    for name, value in _iter_cookies(session):
        if name == "oai-did" and value:
            return value
    return fallback


def _is_transient_login_state_error(message: str) -> bool:
    low = message.lower()
    return "http 409" in low and any(marker in low for marker in _TRANSIENT_STATE_MARKERS)


def _classify_password_error(status: int, body: str) -> LoginError:
    low = body.lower()
    if "mfa_required" in low or "totp_required" in low:
        return LoginError("mfa", "MFA required")
    if status == 403 or any(
        k in low
        for k in (
            "account_locked",
            "account_disabled",
            "account_deactivated",
            "banned",
            "deleted or deactivated",
        )
    ):
        return LoginError("locked", "account locked or deactivated")
    if "password_incorrect" in low or status == 401:
        return LoginError("credential", "invalid email or password")
    return LoginError("network", f"password/verify HTTP {status}: {short(body)}")


def _remember_nav_id(session, resp) -> None:
    nid = resp.headers.get("x-openai-document-navigation-id") or ""
    if nid:
        session._oai_nav_id = nid


def _nav_id(session) -> str:
    return str(getattr(session, "_oai_nav_id", "") or "")


def _follow_html(session, start_url: str, hops: int = 12) -> tuple[str, str]:
    current = start_url
    last_body = ""
    for hop in range(hops):
        if _has_oauth_code(current) and "chatgpt.com/api/auth/callback" in current:
            return current, last_body
        site = "none" if hop == 0 else "same-origin" if "auth.openai.com" in current else "cross-site"
        referer = "https://chatgpt.com/" if "chatgpt.com" in current else f"{AUTH}/"
        resp = session.get(
            current,
            headers=chrome_nav_headers(referer, site),
            allow_redirects=False,
        )
        _remember_nav_id(session, resp)
        loc = resp.headers.get("location") or ""
        if resp.status_code in {301, 302, 303, 307, 308} and loc:
            current = _absolutize(current, loc)
            continue
        last_body = resp.text if resp.status_code == 200 else ""
        return current, last_body
    return current, last_body


def _prime(session) -> None:
    headers = chrome_nav_headers("https://chatgpt.com/", "same-origin")
    for attempt in range(3):
        try:
            session.get(URL_AUTH_LOGIN, headers=headers, allow_redirects=True)
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep((attempt + 1) * 2)


def _csrf(session) -> str:
    headers = chrome_xhr_headers(
        f"{CHATGPT}/auth/login",
        CHATGPT,
        accept="*/*",
        same_origin=True,
        content_type=None,
    )
    last = "csrf exhausted"
    for attempt in range(3):
        resp = session.get(URL_CSRF, headers=headers)
        if resp.status_code == 403 and attempt < 2:
            time.sleep((attempt + 1) * 5)
            last = "csrf HTTP 403"
            continue
        if resp.status_code != 200:
            last = f"csrf HTTP {resp.status_code}: {short(resp.text)}"
            if resp.status_code >= 500 and attempt < 2:
                time.sleep((attempt + 1) * 2)
                continue
            raise LoginError("network", last)
        token = (resp.json() or {}).get("csrfToken")
        if not token:
            raise LoginError("network", "csrf missing csrfToken")
        return str(token)
    raise LoginError("network", last)


def _signin(session, csrf_token: str, device_id: str, email: str) -> str:
    headers = chrome_xhr_headers(
        f"{CHATGPT}/auth/login",
        CHATGPT,
        accept="*/*",
        same_origin=True,
        content_type="application/x-www-form-urlencoded",
    )
    params = {
        "prompt": "login",
        "ext-passkey-client-capabilities": "11111",
        "screen_hint": "login_or_signup",
        "ext-oai-did": device_id,
        "login_hint": email,
    }
    data = {"csrfToken": csrf_token, "callbackUrl": "/", "json": "true"}
    resp = session.post(URL_SIGNIN, headers=headers, params=params, data=data)
    if resp.status_code != 200:
        raise LoginError("network", f"signin HTTP {resp.status_code}: {short(resp.text)}")
    try:
        payload = resp.json() or {}
    except Exception as exc:
        raise LoginError("network", f"signin not json: {short(resp.text)}") from exc
    url = str(payload.get("url") or payload.get("redirect") or "")
    if url.startswith("/"):
        url = CHATGPT + url
    if "auth.openai.com" not in url and "oauth/authorize" not in url:
        raise LoginError("network", f"signin did not return auth.openai.com authorize URL ({short(url or resp.text)})")
    return url


def _password_verify(session, password: str, device_id: str) -> dict[str, Any]:
    sentinel = get_sentinel_token_pow(session, device_id, "password_verify", LOGIN_UA)
    if not sentinel:
        raise LoginError("network", "sentinel token empty for password/verify")
    headers = auth_api_headers(f"{AUTH}/log-in/password", _nav_id(session))
    headers["openai-sentinel-token"] = sentinel
    headers["oai-device-id"] = device_id
    resp = session.post(URL_PASSWORD_VERIFY, headers=headers, json={"password": password})
    if resp.status_code >= 400:
        raise _classify_password_error(resp.status_code, resp.text)
    payload = resp.json()
    page = payload.get("page") or {}
    return {
        "page_type": str(page.get("type") or ""),
        "continue_url": str(payload.get("continue_url") or ""),
        "factor_id": ((page.get("payload") or {}).get("factor_id") if isinstance(page, dict) else None),
        "payload": payload,
    }


def _mfa_verify(session, challenge_id: str, secret: str, device_id: str) -> dict[str, Any]:
    issue_headers = auth_api_headers(f"{AUTH}/log-in/password", _nav_id(session))
    issue_headers["accept"] = "*/*"
    issue_headers["oai-device-id"] = device_id
    try:
        session.post(
            URL_MFA_ISSUE,
            headers=issue_headers,
            json={"id": challenge_id, "type": "totp", "force_fresh_challenge": False},
        )
    except Exception:
        pass

    headers = auth_api_headers(f"{AUTH}/mfa-challenge/{challenge_id}", _nav_id(session))
    headers["oai-device-id"] = device_id
    last = "mfa verify failed"
    for _ in range(3):
        code = generate_code(secret)
        resp = session.post(
            URL_MFA_VERIFY,
            headers=headers,
            json={"id": challenge_id, "type": "totp", "code": code},
        )
        if resp.status_code == 200:
            data = resp.json() or {}
            url = data.get("continue_url") or ""
            if not url:
                raise LoginError("mfa", "mfa/verify missing continue_url")
            return {"continue_url": url, "payload": data}
        last = f"mfa/verify HTTP {resp.status_code}: {short(resp.text)}"
        if _is_transient_login_state_error(last):
            raise LoginError("network", last)
        time.sleep(2)
    raise LoginError("mfa", last)


def _consume_callback(session, url: str) -> None:
    last = "callback missing session-token"
    for _ in range(3):
        session.get(url, headers=chrome_nav_headers(f"{AUTH}/", "cross-site"), allow_redirects=True)
        if _session_token_from_jar(session):
            return
        time.sleep(1)
        last = "callback did not set session-token"
    raise LoginError("network", last)


def _fetch_session(session) -> dict[str, Any]:
    headers = chrome_xhr_headers(CHATGPT + "/", CHATGPT, accept="*/*", same_origin=True)
    resp = session.get(URL_SESSION, headers=headers)
    if resp.status_code != 200:
        raise LoginError("network", f"session HTTP {resp.status_code}: {short(resp.text)}")
    data = resp.json() or {}
    token = data.get("accessToken") or ""
    if not token:
        raise LoginError("network", "session missing accessToken")
    return data


def open_authorize(session, authorize_url: str) -> str:
    landing, _body = _follow_html(session, authorize_url)
    return landing


def submit_auth_password_mfa(session, creds: Credentials, device_id: str) -> dict[str, Any]:
    device_id = _device_id(session, device_id)
    headers = auth_api_headers(f"{AUTH}/log-in", _nav_id(session))
    sentinel = get_sentinel_token_pow(session, device_id, "authorize_continue", LOGIN_UA)
    headers["openai-sentinel-token"] = sentinel
    headers["oai-device-id"] = device_id
    cont = session.post(
        URL_AUTHORIZE_CONTINUE,
        headers=headers,
        json={"username": {"kind": "email", "value": creds.email}},
    )
    if cont.status_code >= 400:
        raise LoginError("network", f"authorize/continue HTTP {cont.status_code}: {short(cont.text)}")

    pv = _password_verify(session, creds.password, device_id)
    continue_url = pv["continue_url"]
    payload = pv.get("payload") or {}
    needs_mfa = "mfa" in pv["page_type"] or "mfa-challenge" in continue_url
    if needs_mfa:
        if not creds.totp_secret:
            raise LoginError("mfa", "MFA required but no totp secret in line")
        challenge_id = pv.get("factor_id") or continue_url.rstrip("/").rsplit("/", 1)[-1]
        mfa = _mfa_verify(session, str(challenge_id), creds.totp_secret, device_id)
        continue_url = mfa["continue_url"]
        payload = mfa.get("payload") or payload
    return {"continue_url": continue_url, "payload": payload}


def login_keep_session(creds: Credentials, proxy: str | None = None, cancel_event=None) -> tuple[SessionBundle, Any]:
    from gpt_tool.oauth import oauth_codex_with_password

    last_err: Exception | None = None
    for attempt in range(3):
        session = build_client(proxy, cancel_event=cancel_event)
        device_id = str(uuid.uuid4())
        try:
            session.cookies.set("oai-did", device_id, domain="auth.openai.com")
        except Exception:
            pass
        try:
            tokens = oauth_codex_with_password(session, creds, device_id)
            session._codex_tokens = tokens
            bundle = SessionBundle(
                email=creds.email,
                access_token=tokens.access_token or "",
                session_token=_session_token_from_jar(session),
                device_id=_device_id(session, device_id),
                cookies={},
            )
            return bundle, session
        except InterruptedError:
            raise
        except LoginError as exc:
            last_err = exc
            retryable = _is_transient_login_state_error(str(exc))
            if not retryable or attempt == 2:
                raise LoginError(exc.kind, redact(str(exc))) from exc
            if cancel_event is not None and cancel_event.wait(2):
                raise InterruptedError("Đã dừng")
            time.sleep(0 if cancel_event is not None else 2)
        except Exception as exc:
            last_err = LoginError("network", redact(str(exc)))
            if attempt == 2:
                raise last_err from exc
            if cancel_event is not None and cancel_event.wait(2):
                raise InterruptedError("Đã dừng")
            time.sleep(0 if cancel_event is not None else 2)
    raise last_err or LoginError("network", "login failed")
