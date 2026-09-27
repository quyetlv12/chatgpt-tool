"""Pure-request password mutation for an already authenticated OpenAI account.

The endpoint contract mirrors the current official auth.openai.com web client:
reauthenticate the current password (and TOTP when requested), then submit the
new password through ``/api/accounts/password/reset``.  No browser dependency
is imported or used by this module.
"""
from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit


LogFn = Callable[[str], None]
_AUTH_BASE = "https://auth.openai.com"
_RESET_REJECTION_MESSAGES = {
    "password_already_used": (
        "Mật khẩu mới đã từng được sử dụng; hãy chọn mật khẩu khác"
    ),
    "password_contains_user_info": (
        "Mật khẩu mới chứa thông tin cá nhân; hãy chọn mật khẩu khác"
    ),
    "password_too_weak": (
        "Mật khẩu mới quá yếu theo chính sách OpenAI; hãy dùng mật khẩu khó đoán hơn"
    ),
    "string_above_max_length": (
        "Mật khẩu mới vượt quá độ dài OpenAI cho phép"
    ),
}


class PasswordMutationError(RuntimeError):
    """Base error for the private auth-web password operation."""


class PasswordMutationRejected(PasswordMutationError):
    """The server definitively rejected the request."""


class PasswordMutationUncertain(PasswordMutationError):
    """The mutation may have reached the server; it must not be resent blindly."""


def _cookie_value(session: Any, name: str) -> str:
    try:
        value = session.cookies.get(name)
    except Exception:
        return ""
    return str(value or "")


def _inject_cookies(session: Any, cookies: Any) -> None:
    if not isinstance(cookies, list):
        raise PasswordMutationRejected("Phiên đăng nhập không có cookie xác thực")
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        domain = item.get("domain")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        kwargs: dict[str, str] = {"path": str(item.get("path") or "/")}
        if isinstance(domain, str) and domain:
            kwargs["domain"] = domain
        try:
            session.cookies.set(name, value, **kwargs)
        except Exception:
            # A malformed non-auth cookie should not prevent reauthentication.
            continue


def _page_state(response: Any) -> tuple[str, str]:
    try:
        payload = response.json()
    except Exception:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    page = payload.get("page")
    page_type = str(page.get("type") or "") if isinstance(page, dict) else ""
    continue_url = str(payload.get("continue_url") or "")
    return page_type.strip(), continue_url.strip()


def _safe_page_type(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if re.fullmatch(r"[a-z0-9_]{1,64}", normalized):
        return normalized
    return "unknown"


def _safe_auth_path(value: Any) -> str:
    """Return only a whitelisted auth route class, never query data or IDs."""
    raw = getattr(value, "url", value)
    try:
        parsed = urlsplit(str(raw or ""))
    except Exception:
        return "unknown"
    if parsed.hostname and parsed.hostname.casefold() != "auth.openai.com":
        return "other-origin"
    path = parsed.path.rstrip("/") or "/"
    known = {
        "/log-in/password",
        "/reset-password/new-password",
        "/reset-password",
        "/password/add",
    }
    if path in known:
        return path
    if path.startswith("/mfa-challenge/"):
        return "/mfa-challenge/:id"
    return "other-auth-path"


def _reset_rejection_message(response: Any) -> str:
    """Translate only known policy codes; never expose upstream response text."""
    try:
        payload = response.json()
    except Exception:
        payload = None
    candidates: list[Any] = []
    if isinstance(payload, dict):
        candidates.append(payload.get("error"))
        data = payload.get("data")
        if isinstance(data, dict):
            candidates.append(data.get("error"))
    code = next(
        (
            item.get("code")
            for item in candidates
            if isinstance(item, dict) and isinstance(item.get("code"), str)
        ),
        None,
    )
    if isinstance(code, str):
        normalized = code.casefold()
        if normalized in _RESET_REJECTION_MESSAGES:
            return _RESET_REJECTION_MESSAGES[normalized]
        if re.fullmatch(r"[a-z0-9_]{1,64}", normalized):
            return (
                "OpenAI từ chối mật khẩu mới "
                f"(HTTP {response.status_code}, mã {normalized})"
            )
    return f"OpenAI từ chối mật khẩu mới (HTTP {response.status_code})"


def _safe_headers(referer: str) -> dict[str, str]:
    from request_phase import _common_headers

    headers = _common_headers(referer)
    headers["Content-Type"] = "application/json"
    return headers


def _sentinel_headers(
    session: Any,
    *,
    device_id: str,
    flow: str,
    referer: str,
    log: LogFn,
) -> dict[str, str]:
    from request_phase import _get_sentinel_token

    headers = _safe_headers(referer)
    if device_id:
        headers["oai-device-id"] = device_id
    token = _get_sentinel_token(session, device_id, flow, log)
    if token:
        headers["openai-sentinel-token"] = token
    return headers


def _verify_reauthentication_mfa(
    session: Any,
    *,
    page_type: str,
    continue_url: str,
    secret: str,
    device_id: str,
    log: LogFn,
) -> tuple[str, str]:
    if "mfa" not in page_type.casefold() and "mfa" not in continue_url.casefold():
        return page_type, continue_url
    if not secret:
        raise PasswordMutationRejected("Tài khoản yêu cầu 2FA nhưng input không có secret")

    match = re.search(r"/mfa-challenge/([a-f0-9]+)", continue_url, re.IGNORECASE)
    if not match:
        raise PasswordMutationRejected("Không đọc được MFA challenge của phiên đổi mật khẩu")
    challenge_id = match.group(1)
    headers = _safe_headers(f"{_AUTH_BASE}/mfa-challenge")
    if device_id:
        headers["oai-device-id"] = device_id

    issue = session.post(
        f"{_AUTH_BASE}/api/accounts/mfa/issue_challenge",
        headers=headers,
        json={"id": challenge_id, "type": "totp", "force_fresh_challenge": False},
        timeout=30,
    )
    if issue.status_code not in {200, 409}:
        raise PasswordMutationRejected(
            f"Không khởi tạo được xác minh 2FA (HTTP {issue.status_code})"
        )

    from totp_helper import generate_code

    verify = session.post(
        f"{_AUTH_BASE}/api/accounts/mfa/verify",
        headers=headers,
        json={
            "id": challenge_id,
            "type": "totp",
            "code": generate_code(secret),
        },
        timeout=30,
    )
    if verify.status_code != 200:
        raise PasswordMutationRejected(
            f"Xác minh 2FA cho đổi mật khẩu thất bại (HTTP {verify.status_code})"
        )
    log("[password] Đã xác minh 2FA cho thao tác bảo mật")
    return _page_state(verify)


def _start_password_reauthentication(
    session: Any,
    *,
    device_id: str,
    login_hint: str,
    log: LogFn,
) -> str:
    """Create the same password-reset re-auth intent as ChatGPT Settings."""
    from request_phase import _step_auth_url, _step_csrf, _step_oauth_init

    # request_phase logs include bootstrap details useful for account creation,
    # including a CSRF prefix. Password jobs keep this security-sensitive
    # bootstrap silent and expose only the resulting safe state below.
    quiet_log = lambda _line: None
    try:
        csrf = _step_csrf(session, quiet_log)
        auth_url = _step_auth_url(
            session,
            csrf,
            quiet_log,
            device_id=device_id,
            login_hint=login_hint,
            auth_session_logging_id=str(uuid.uuid4()),
            authorization_params={
                "reauth": "password",
                "max_age": "0",
                "post_login_password_reset": "true",
            },
        )
        canonical_device_id = _step_oauth_init(
            session,
            auth_url,
            quiet_log,
            expected_device_id=device_id,
            expected_path="/log-in/password",
        )
    except Exception as exc:
        raise PasswordMutationRejected(
            "Không khởi tạo được phiên xác minh dành cho đổi mật khẩu"
        ) from exc
    log("[password] Đã khởi tạo phiên re-auth dành cho đổi mật khẩu")
    return canonical_device_id


def _change_password_sync(
    *,
    session_data: dict[str, Any],
    current_password: str,
    new_password: str,
    secret: str,
    log: LogFn,
    _submit_reset: bool = True,
) -> None:
    from request_phase import _create_session

    if not current_password or not new_password or current_password == new_password:
        raise PasswordMutationRejected("Mật khẩu mới không hợp lệ hoặc trùng mật khẩu cũ")

    session = _create_session(proxy=None)
    try:
        _inject_cookies(session, session_data.get("__cookies"))
        device_id = (
            _cookie_value(session, "oai-did")
            or str(session_data.get("deviceId") or session_data.get("device_id") or "")
        )
        session_user = session_data.get("user")
        login_hint = (
            str(session_user.get("email") or "")
            if isinstance(session_user, dict)
            else ""
        )
        device_id = _start_password_reauthentication(
            session,
            device_id=device_id,
            login_hint=login_hint,
            log=log,
        )
        navigation_headers = _safe_headers("https://chatgpt.com/#settings/Account")
        navigation_headers.pop("Content-Type", None)
        navigation_headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-site",
            "Upgrade-Insecure-Requests": "1",
        })
        # _step_oauth_init already performed and validated the top-level
        # navigation. Reopening /log-in/password here starts a second document
        # transition and current auth.openai.com rejects it with HTTP 400.
        log("[password-diag] landing status=200 path=/log-in/password validated=true")

        verify_headers = _sentinel_headers(
            session,
            device_id=device_id,
            flow="password_verify",
            referer=f"{_AUTH_BASE}/log-in/password",
            log=log,
        )
        verified = session.post(
            f"{_AUTH_BASE}/api/accounts/password/verify",
            headers=verify_headers,
            json={"password": current_password},
            timeout=30,
        )
        if verified.status_code != 200:
            raise PasswordMutationRejected(
                f"Mật khẩu hiện tại bị từ chối (HTTP {verified.status_code})"
            )
        log("[password] Đã re-auth bằng mật khẩu hiện tại")
        page_type, continue_url = _page_state(verified)
        log(
            "[password-diag] password_verified "
            f"page_type={_safe_page_type(page_type)} "
            f"continue_path={_safe_auth_path(continue_url)}"
        )
        page_type, continue_url = _verify_reauthentication_mfa(
            session,
            page_type=page_type,
            continue_url=continue_url,
            secret=secret,
            device_id=device_id,
            log=log,
        )
        log(
            "[password-diag] reauth_complete "
            f"page_type={_safe_page_type(page_type)} "
            f"continue_path={_safe_auth_path(continue_url)}"
        )

        normalized_page_type = _safe_page_type(page_type)
        continue_path = _safe_auth_path(continue_url)
        if (
            normalized_page_type != "reset_password_new_password"
            and continue_path != "/reset-password/new-password"
        ):
            raise PasswordMutationRejected(
                "Phiên xác minh chưa sẵn sàng để đặt mật khẩu mới"
            )
        if continue_url and continue_path != "/reset-password/new-password":
            raise PasswordMutationRejected(
                "Đường dẫn đặt mật khẩu mới không hợp lệ"
            )

        target = (
            urljoin(_AUTH_BASE, continue_url)
            if continue_url
            else f"{_AUTH_BASE}/reset-password/new-password"
        )
        response = session.get(
            target,
            headers=navigation_headers,
            timeout=30,
            allow_redirects=True,
        )
        if response.status_code >= 400:
            raise PasswordMutationRejected(
                f"Không mở được form mật khẩu mới (HTTP {response.status_code})"
            )
        log(
            "[password-diag] continue_landing "
            f"status={response.status_code} path={_safe_auth_path(response)} "
            f"redirects={len(getattr(response, 'history', ()) or ())}"
        )
        if _safe_auth_path(response) != "/reset-password/new-password":
            raise PasswordMutationRejected(
                "Phiên xác minh không vào đúng form đặt mật khẩu mới"
            )

        if not _submit_reset:
            log("[password-diag] preflight complete; reset not submitted")
            return

        reset_headers = _sentinel_headers(
            session,
            device_id=device_id,
            flow="password_reset",
            referer=f"{_AUTH_BASE}/reset-password/new-password",
            log=log,
        )
        log("[password] Gửi yêu cầu cập nhật mật khẩu một lần")
        try:
            changed = session.post(
                f"{_AUTH_BASE}/api/accounts/password/reset",
                headers=reset_headers,
                json={"password": new_password},
                timeout=30,
                allow_redirects=False,
            )
        except Exception as exc:
            raise PasswordMutationUncertain(
                "Mất kết nối sau khi gửi yêu cầu đổi mật khẩu"
            ) from exc

        if 400 <= changed.status_code < 500 and changed.status_code != 408:
            raise PasswordMutationRejected(_reset_rejection_message(changed))
        if not 200 <= changed.status_code < 300:
            raise PasswordMutationUncertain(
                f"Phản hồi đổi mật khẩu không xác định (HTTP {changed.status_code})"
            )
        log("[password] Server đã nhận yêu cầu; bắt buộc đăng nhập mới để xác minh")
    finally:
        try:
            session.close()
        except Exception:
            pass


async def change_password_pure_request(
    *,
    session_data: dict[str, Any],
    current_password: str,
    new_password: str,
    secret: str,
    log: LogFn = print,
) -> None:
    """Change a password through HTTP only; a fresh login must verify success."""
    await asyncio.to_thread(
        _change_password_sync,
        session_data=session_data,
        current_password=current_password,
        new_password=new_password,
        secret=secret,
        log=log,
    )
