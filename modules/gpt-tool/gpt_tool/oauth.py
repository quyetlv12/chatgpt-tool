"""Codex OAuth PKCE — no SMS / no add-phone handler."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from gpt_tool.http_client import chrome_nav_headers, chrome_xhr_headers
from gpt_tool.jwtutil import decode_payload, openai_auth
from gpt_tool.redaction import short

_US_ID = re.compile(r"us_[A-Za-z0-9]{16,}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_WORKSPACE_JSON = re.compile(r'"workspace_id"\s*:\s*"([^"]+)"')
_WORKSPACE_LIST_ID = re.compile(r'"workspaces"\s*:\s*\[\s*\{\s*"id"\s*:\s*"([^"]+)"')

CODEX_CLIENT_ID = os.environ.get("OAUTH_CODEX_CLIENT_ID") or "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_REDIRECT_URI = os.environ.get("OAUTH_CODEX_REDIRECT_URI") or "http://localhost:1455/auth/callback"
CODEX_SCOPE = os.environ.get("OAUTH_CODEX_SCOPE") or "openid email profile offline_access"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"

ADD_PHONE_MSG = (
    "Tài khoản này vẫn bị OpenAI yêu cầu add-phone khi OAuth Codex. "
    "Tool không thuê SIM / Veriphone. Bỏ qua tài khoản này. "
    "This account still hit Codex add-phone; SMS is intentionally disabled."
)


class OAuthError(Exception):
    pass


class AddPhoneRequired(OAuthError):
    pass


@dataclass
class CodexAuthorize:
    auth_url: str
    state: str
    verifier: str
    redirect_uri: str
    client_id: str


@dataclass
class CodexTokens:
    access_token: str | None
    refresh_token: str | None
    id_token: str | None


def is_add_phone_url(url: str) -> bool:
    u = (url or "").lower()
    return "/add-phone" in u or "add_phone" in u


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def build_codex_authorize(prompt: str = "login") -> CodexAuthorize:
    state = _b64url(secrets.token_bytes(24))
    verifier = _b64url(secrets.token_bytes(32))
    if len(verifier) < 43:
        verifier = (verifier + "A" * 43)[:43]
    elif len(verifier) > 128:
        verifier = verifier[:128]
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    query = {
        "client_id": CODEX_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": CODEX_REDIRECT_URI,
        "scope": CODEX_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "audience": "https://api.openai.com/v1",
        "prompt": prompt,
    }
    return CodexAuthorize(
        auth_url=f"{AUTHORIZE_URL}?{urlencode(query)}",
        state=state,
        verifier=verifier,
        redirect_uri=CODEX_REDIRECT_URI,
        client_id=CODEX_CLIENT_ID,
    )


def drop_query_keys(url: str, drop: list[str]) -> str:
    parsed = urlparse(url)
    q = parse_qs(parsed.query, keep_blank_values=True)
    for key in drop:
        q.pop(key, None)
    new_query = urlencode({k: v[0] if len(v) == 1 else v for k, v in q.items()}, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def callback_has_code(url: str, redirect_uri: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    cb = redirect_uri.split("?")[0].rstrip("/")
    target = urlunparse(parsed._replace(query="", fragment="")).rstrip("/")
    if cb and target != cb:
        return False
    return bool(parse_qs(parsed.query).get("code", [""])[0].strip())


def _absolutize(current: str, loc: str) -> str:
    if loc.startswith("http://") or loc.startswith("https://"):
        return loc
    joined = urljoin(current, loc)
    if joined.startswith("http"):
        return joined
    if loc.startswith("/"):
        return "https://auth.openai.com" + loc
    return loc


def extract_session_id_from_html(html: str) -> str | None:
    m = _US_ID.search(html or "")
    return m.group(0) if m else None


def extract_workspace_id_from_html(html: str) -> str | None:
    m = _WORKSPACE_LIST_ID.search(html or "")
    if m:
        return m.group(1)
    m = _WORKSPACE_JSON.search(html or "")
    if m:
        return m.group(1)
    m = _UUID.search(html or "")
    if m:
        return m.group(0)
    m = _US_ID.search(html or "")
    return m.group(0) if m else None


def extract_workspace_id_from_payload(payload: dict) -> str | None:
    sess = payload.get("oai-client-auth-session") if isinstance(payload, dict) else None
    buckets = []
    if isinstance(sess, dict):
        buckets.append(sess.get("workspaces"))
    buckets.append(payload.get("workspaces") if isinstance(payload, dict) else None)
    for workspaces in buckets:
        if isinstance(workspaces, list) and workspaces:
            first = workspaces[0]
            if isinstance(first, dict) and first.get("id"):
                return str(first["id"])
    return extract_workspace_id_from_html(json.dumps(payload) if payload else "")


def _b64_json_segments(raw: str):
    for segment in raw.split(".")[:2]:
        if not segment:
            continue
        s = segment.replace("-", "+").replace("_", "/")
        s += "=" * ((4 - len(s) % 4) % 4)
        try:
            data = json.loads(base64.b64decode(s))
        except Exception:
            try:
                data = json.loads(base64.urlsafe_b64decode(s + "=" * ((4 - len(s) % 4) % 4)))
            except Exception:
                continue
        if isinstance(data, dict):
            yield data


def parse_workspace_from_session_cookie(raw: str) -> str | None:
    for data in _b64_json_segments(raw):
        wid = data.get("workspace_id")
        if isinstance(wid, str) and wid:
            return wid
        workspaces = data.get("workspaces")
        if isinstance(workspaces, list):
            for item in workspaces:
                if isinstance(item, dict) and item.get("id"):
                    return str(item["id"])
    return None


def _cookie_value(session, name: str) -> str | None:
    jar = getattr(session.cookies, "jar", None)
    if jar is not None:
        for cookie in jar:
            if cookie.name == name and cookie.value:
                return cookie.value
    try:
        value = session.cookies.get(name)
        return value or None
    except Exception:
        return None


def extract_workspace_id(session, html: str = "") -> str | None:
    raw = _cookie_value(session, "oai-client-auth-session")
    if raw:
        wid = parse_workspace_from_session_cookie(raw)
        if wid:
            return wid
    return extract_workspace_id_from_html(html)


def _continue_url(payload: dict) -> str | None:
    for key in ("continue_url", "next", "url", "redirect_url"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def choose_account_select(session, html: str) -> str | None:
    session_id = extract_session_id_from_html(html)
    if not session_id:
        return None
    resp = session.post(
        "https://auth.openai.com/api/accounts/session/select",
        headers=chrome_xhr_headers(
            "https://auth.openai.com/choose-an-account",
            "https://auth.openai.com",
            same_origin=True,
        ),
        json={"session_id": session_id},
    )
    if resp.status_code >= 400:
        return None
    try:
        return _continue_url(resp.json() or {})
    except Exception:
        return None


def workspace_select(session, workspace_id: str) -> str | None:
    resp = session.post(
        "https://auth.openai.com/api/accounts/workspace/select",
        headers=chrome_xhr_headers(
            "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            "https://auth.openai.com",
            same_origin=True,
        ),
        json={"workspace_id": workspace_id},
    )
    if resp.status_code >= 400:
        return None
    try:
        return _continue_url(resp.json() or {})
    except Exception:
        return None


def follow_authorize_for_callback(session, start_url: str, redirect_uri: str) -> tuple[str, str]:
    current = start_url
    callback = ""
    chose_account = False
    headers = chrome_nav_headers("https://auth.openai.com/", "none")
    for hop in range(16):
        if callback_has_code(current, redirect_uri):
            return current, current
        site = "none" if hop == 0 else "same-origin"
        headers = chrome_nav_headers("https://auth.openai.com/sign-in-with-chatgpt/codex/consent", site)
        resp = session.get(current, headers=headers, allow_redirects=False)
        loc = resp.headers.get("location") or ""
        body = resp.text if resp.status_code == 200 else ""
        if resp.status_code == 200:
            workspace_like = (
                "/workspace" in current
                or "/sign-in-with-chatgpt/" in current
                or "/consent" in current
                or "codex/consent" in current
            )
            if workspace_like:
                wid = extract_workspace_id(session, body)
                if wid:
                    nxt = workspace_select(session, wid)
                    if nxt:
                        current = _absolutize(current, nxt)
                        continue
            if "/choose-an-account" in current and not chose_account:
                chose_account = True
                nxt = choose_account_select(session, body)
                if nxt:
                    current = _absolutize(current, nxt)
                    continue
            return callback, current
        if resp.status_code not in {301, 302, 303, 307, 308} or not loc:
            return callback, current
        nxt = _absolutize(current, loc)
        if callback_has_code(nxt, redirect_uri):
            return nxt, nxt
        current = nxt
    return callback, current


def exchange_codex_callback_code(
    session,
    callback_url: str,
    expected_state: str,
    verifier: str,
    redirect_uri: str,
    client_id: str,
) -> CodexTokens:
    q = parse_qs(urlparse(callback_url).query)
    code = (q.get("code") or [""])[0]
    got_state = (q.get("state") or [""])[0]
    if not code.strip():
        raise OAuthError("codex callback missing code")
    if expected_state and got_state and got_state != expected_state:
        raise OAuthError("codex callback state mismatch")
    headers = chrome_xhr_headers(
        "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        "https://auth.openai.com",
        same_origin=True,
    )
    headers["content-type"] = "application/x-www-form-urlencoded"
    resp = session.post(
        TOKEN_URL,
        headers=headers,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code.strip(),
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
    )
    if resp.status_code >= 400:
        raise OAuthError(f"codex oauth/token HTTP {resp.status_code}: {short(resp.text)}")
    data = resp.json()
    rt = data.get("refresh_token") or ""
    if not rt:
        raise OAuthError("codex token response missing refresh_token")
    return CodexTokens(
        access_token=data.get("access_token"),
        refresh_token=rt,
        id_token=data.get("id_token"),
    )


def refresh_codex_token(refresh_token: str, proxy: str | None = None, cancel_event=None) -> CodexTokens:
    from gpt_tool.http_client import build_client

    rt = refresh_token.strip()
    if not rt:
        raise OAuthError("missing refresh_token")
    session = build_client(proxy, cancel_event=cancel_event)
    headers = chrome_xhr_headers(
        "https://auth.openai.com/",
        "https://auth.openai.com",
        same_origin=True,
    )
    headers["content-type"] = "application/x-www-form-urlencoded"
    resp = session.post(
        TOKEN_URL,
        headers=headers,
        data={
            "grant_type": "refresh_token",
            "client_id": CODEX_CLIENT_ID,
            "refresh_token": rt,
            "scope": CODEX_SCOPE,
        },
    )
    if resp.status_code >= 400:
        raise OAuthError(f"OpenAI token refresh HTTP {resp.status_code}: {short(resp.text)}")
    data = resp.json()
    access = data.get("access_token") or ""
    if not access:
        raise OAuthError("refresh response missing access_token")
    return CodexTokens(
        access_token=access,
        refresh_token=data.get("refresh_token") or rt,
        id_token=data.get("id_token"),
    )


def _finish_codex_callback(session, auth: CodexAuthorize, start_url: str) -> CodexTokens:
    callback, final_url = follow_authorize_for_callback(session, start_url, auth.redirect_uri)
    if not callback and is_add_phone_url(final_url):
        raise AddPhoneRequired(f"{ADD_PHONE_MSG} ({short(final_url, 120)})")
    if not callback:
        hint = ""
        if "choose-an-account" in (final_url or "").lower():
            hint = " (kẹt trang chọn tài khoản)"
        elif "/log-in" in (final_url or "").lower():
            hint = " (kẹt /log-in — password/MFA chưa xong trên cùng PKCE)"
        raise OAuthError(f"codex OAuth no callback code, final={short(final_url, 180)}{hint}")
    return exchange_codex_callback_code(
        session,
        callback,
        auth.state,
        auth.verifier,
        auth.redirect_uri,
        auth.client_id,
    )


def oauth_codex_with_password(session, creds, device_id: str) -> CodexTokens:
    """One capture flow: authorize → password → MFA → workspace → callback."""
    from gpt_tool.login import LoginError, open_authorize, submit_auth_password_mfa

    auth = build_codex_authorize("login")
    landing = open_authorize(session, auth.auth_url)
    low = (landing or "").lower()
    if callback_has_code(landing, auth.redirect_uri):
        return exchange_codex_callback_code(
            session, landing, auth.state, auth.verifier, auth.redirect_uri, auth.client_id
        )
    if is_add_phone_url(landing):
        raise AddPhoneRequired(f"{ADD_PHONE_MSG} ({short(landing, 120)})")
    if "/email-verification" in low:
        raise LoginError("credential", "passwordless / email-verification accounts are not supported")
    if "/log-in" not in low and "password" not in low:
        callback, final_url = follow_authorize_for_callback(session, landing, auth.redirect_uri)
        if callback:
            return exchange_codex_callback_code(
                session, callback, auth.state, auth.verifier, auth.redirect_uri, auth.client_id
            )
        raise OAuthError(f"codex OAuth unexpected page, final={short(final_url or landing, 180)}")

    result = submit_auth_password_mfa(session, creds, device_id)
    nxt = result.get("continue_url") or landing
    wid = extract_workspace_id_from_payload(result.get("payload") or {}) or extract_workspace_id(session)
    if wid:
        selected = workspace_select(session, wid)
        if selected:
            nxt = selected
    return _finish_codex_callback(session, auth, nxt)


def oauth_codex_rt_exchange(session, device_id: str, creds=None) -> CodexTokens:
    cached = getattr(session, "_codex_tokens", None)
    if cached and getattr(cached, "refresh_token", None):
        return cached
    if creds is not None:
        return oauth_codex_with_password(session, creds, device_id)
    auth = build_codex_authorize("login")
    return _finish_codex_callback(session, auth, auth.auth_url)


def account_id_from_access(access_token: str) -> str:
    auth = openai_auth(decode_payload(access_token))
    return str(auth.get("chatgpt_account_id") or auth.get("account_id") or "")
