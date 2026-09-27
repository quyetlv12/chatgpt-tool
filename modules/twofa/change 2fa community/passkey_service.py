"""One-use browser handoff. WebAuthn private keys never enter the tool."""
import secrets
import time
from urllib.parse import parse_qs, urlsplit


def validate_passkey_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) <= 32 for c in url):
        raise ValueError("Invalid passkey handoff")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or parsed.netloc != "auth.openai.com"
            or parsed.path != "/passkey-enroll" or parsed.fragment
            or set(query) != {"state"} or len(query["state"]) != 1
            or not query["state"][0]
            or any(ord(c) < 32 for c in query["state"][0])):
        raise ValueError("Invalid passkey handoff")
    return url


async def prepare_passkey_url(*, session_data: dict, timeout: float = 20.0) -> str:
    """Use the web client's enrollment route; never guess an auth state token."""
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
        response = await session.get(
            "https://chatgpt.com/auth/enroll_mfa", params={"factor": "passkey"},
            headers={"Accept": "text/html", "User-Agent": WINDOWS_USER_AGENT,
                     "Referer": "https://chatgpt.com/"},
            timeout=timeout, allow_redirects=False,
        )
    if response.status_code not in {302, 303, 307}:
        raise ValueError("Passkey enrollment redirect unavailable")
    return validate_passkey_url(response.headers.get("Location", ""))


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
