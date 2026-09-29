"""One-use browser handoff. WebAuthn private keys never enter the tool."""
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit


class PasskeyPreparationError(ValueError):
    """Fixed, credential-safe enrollment errors suitable for the dashboard."""


def validate_passkey_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) <= 32 for c in url):
        raise ValueError("Invalid passkey handoff")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or parsed.netloc != "auth.openai.com"
            or parsed.path != "/passkey-enroll" or parsed.fragment
            or set(query) != {"origin_app_name", "mfa_token"}
            or query["origin_app_name"] != ["ChatGPT"]
            or len(query["mfa_token"]) != 1 or not query["mfa_token"][0]
            or any(ord(c) <= 32 or ord(c) == 127 for c in query["mfa_token"][0])):
        raise ValueError("Invalid passkey handoff")
    return url


async def prepare_passkey_url(*, session_data: dict, timeout: float = 20.0) -> str:
    """Mirror BrowserMfaEnrollPage: request an MFA token, not an HTML redirect."""
    from curl_cffi.requests import AsyncSession
    from user_agent_profile import CURL_IMPERSONATE_PRIMARY, WINDOWS_USER_AGENT

    token = session_data.get("accessToken")
    cookies = session_data.get("__cookies")
    if not isinstance(token, str) or not token.strip() or not isinstance(cookies, list):
        raise ValueError("Authenticated session required")
    cookies = [c for c in cookies if isinstance(c, dict)
               and c.get("domain") in {"chatgpt.com", ".chatgpt.com"}
               and isinstance(c.get("name"), str) and isinstance(c.get("value"), str)]
    if not any(c["name"] in {"__Secure-next-auth.session-token", "__Secure-next-auth.session-token.0"}
               and c["value"] for c in cookies):
        raise ValueError("Authenticated session cookie required")
    async with AsyncSession(impersonate=CURL_IMPERSONATE_PRIMARY) as session:
        for cookie in cookies:
            session.cookies.set(cookie["name"], cookie["value"],
                                domain=cookie["domain"], path=cookie.get("path") or "/")
        response = await session.post(
            "https://chatgpt.com/backend-api/accounts/mfa/user/request_mfa_token_in_house",
            headers={"Accept": "application/json", "User-Agent": WINDOWS_USER_AGENT,
                     "Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Origin": "https://chatgpt.com",
                     "Referer": "https://chatgpt.com/auth/enroll_mfa?factor=passkey"},
            timeout=timeout, allow_redirects=False,
        )
    if response.status_code != 200:
        raise PasskeyPreparationError(
            f"OpenAI từ chối bước lấy liên kết passkey (HTTP {response.status_code}). "
            "Hãy thử lại hoặc mở Settings > Security của ChatGPT."
        )
    try:
        payload = response.json()
        state_token = payload.get("state_token") if isinstance(payload, dict) else None
        if not isinstance(state_token, str) or not state_token:
            raise ValueError("Missing MFA token")
        return validate_passkey_url("https://auth.openai.com/passkey-enroll?" + urlencode({
            "origin_app_name": "ChatGPT", "mfa_token": state_token,
        }))
    except (TypeError, ValueError) as exc:
        raise PasskeyPreparationError("OpenAI không trả về liên kết đăng ký passkey hợp lệ.") from exc


class PasskeyHandoffs:
    """Small bounded RAM-only store; expired links are discarded, never replayed."""
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.links: dict[str, tuple[float, str]] = {}

    def issue(self, url: str) -> str:
        validate_passkey_url(url)
        now = self.clock()
        self.links = {key: value for key, value in self.links.items() if value[0] > now}
        if len(self.links) >= 32:
            raise ValueError("Too many pending passkey handoffs")
        token = secrets.token_urlsafe(32)
        self.links[token] = (now + 120, url)
        return token

    def consume(self, token: str) -> str:
        expires, url = self.links.pop(token)
        if expires <= self.clock():
            raise KeyError(token)
        return validate_passkey_url(url)
