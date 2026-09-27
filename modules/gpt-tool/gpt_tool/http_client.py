"""Chrome TLS client via curl_cffi — full browser headers, no trimming."""

from __future__ import annotations

import uuid

from curl_cffi import requests

DEFAULT_TIMEOUT = 30
ACCEPT_LANGUAGE = "vi-VN,vi;q=0.9,fr-FR;q=0.8,fr;q=0.7,en-US;q=0.6,en;q=0.5"
ACCEPT_ENCODING = "gzip, deflate, br, zstd"
ACCEPT_HTML = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/apng,*/*;q=0.8,"
    "application/signed-exchange;v=b3;q=0.7"
)
# Newest first. Must exist in this curl_cffi build (0.14 max is chrome142).
_IMPERSONATE_PREF = (
    "chrome142",
    "chrome136",
    "chrome133a",
    "chrome131",
    "chrome124",
    "chrome120",
)


def _supported_chrome() -> set[str]:
    names: set[str] = set()
    try:
        from curl_cffi.requests.impersonate import BrowserType, DEFAULT_CHROME

        names.update(m.value for m in BrowserType if str(m.value).startswith("chrome"))
        if DEFAULT_CHROME:
            names.add(str(DEFAULT_CHROME))
    except Exception:
        pass
    return names


def _chrome_major(name: str) -> str:
    digits = "".join(ch for ch in name if ch.isdigit())
    return digits[:3] if len(digits) >= 3 else digits or "136"


def _ua_for(major: str) -> str:
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def _sec_ch_ua_for(major: str) -> str:
    return f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not)A;Brand";v="24"'


def _apply_profile(name: str) -> None:
    global IMPERSONATE, LOGIN_UA, SEC_CH_UA
    IMPERSONATE = name
    major = _chrome_major(name)
    LOGIN_UA = _ua_for(major)
    SEC_CH_UA = _sec_ch_ua_for(major)


def pick_impersonate() -> str:
    allowed = _supported_chrome()
    for name in _IMPERSONATE_PREF:
        if allowed and name not in allowed:
            continue
        return name
    return "chrome136"


IMPERSONATE = pick_impersonate()
LOGIN_UA = _ua_for(_chrome_major(IMPERSONATE))
SEC_CH_UA = _sec_ch_ua_for(_chrome_major(IMPERSONATE))


def _decorate(session: requests.Session, timeout: int, proxy: str | None, cancel_event=None) -> requests.Session:
    session.headers["User-Agent"] = LOGIN_UA
    session.headers["sec-ch-ua"] = SEC_CH_UA
    session.headers["sec-ch-ua-mobile"] = "?0"
    session.headers["sec-ch-ua-platform"] = '"macOS"'
    session.headers["accept-language"] = ACCEPT_LANGUAGE
    session.headers["accept-encoding"] = ACCEPT_ENCODING
    session.timeout = timeout
    if proxy and proxy.strip():
        p = proxy.strip()
        session.proxies = {"http": p, "https": p}
    if cancel_event is not None:
        request = session.request

        def cancellable_request(*args, **kwargs):
            if cancel_event.is_set():
                raise InterruptedError("Đã dừng")
            return request(*args, **kwargs)

        session.request = cancellable_request
    return session


def build_client(proxy: str | None = None, timeout: int = DEFAULT_TIMEOUT, cancel_event=None) -> requests.Session:
    last_err: Exception | None = None
    tried = []
    for name in (IMPERSONATE, *_IMPERSONATE_PREF):
        if name in tried:
            continue
        tried.append(name)
        try:
            session = requests.Session(impersonate=name)
        except Exception as exc:
            last_err = exc
            continue
        _apply_profile(name)
        return _decorate(session, timeout, proxy, cancel_event)
    raise last_err or RuntimeError("no supported chrome impersonate")


def chrome_nav_headers(referer: str | None = None, fetch_site: str = "none") -> dict[str, str]:
    headers = {
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "upgrade-insecure-requests": "1",
        "user-agent": LOGIN_UA,
        "accept": ACCEPT_HTML,
        "sec-fetch-site": fetch_site,
        "sec-fetch-mode": "navigate",
        "sec-fetch-user": "?1",
        "sec-fetch-dest": "document",
        "accept-encoding": ACCEPT_ENCODING,
        "accept-language": ACCEPT_LANGUAGE,
        "priority": "u=0, i",
    }
    if referer:
        headers["referer"] = referer
    return headers


def chrome_xhr_headers(
    referer: str,
    origin: str,
    *,
    accept: str = "application/json",
    same_origin: bool = True,
    content_type: str | None = "application/json",
) -> dict[str, str]:
    headers = {
        "sec-ch-ua-platform": '"macOS"',
        "user-agent": LOGIN_UA,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "accept": accept,
        "origin": origin,
        "sec-fetch-site": "same-origin" if same_origin else "same-site",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "referer": referer,
        "accept-encoding": ACCEPT_ENCODING,
        "accept-language": ACCEPT_LANGUAGE,
        "priority": "u=1, i",
    }
    if content_type:
        headers["content-type"] = content_type
    return headers


def auth_api_headers(referer: str, nav_id: str | None = None) -> dict[str, str]:
    headers = chrome_xhr_headers(referer, "https://auth.openai.com", same_origin=True)
    headers["x-access-flow-invocation-id"] = str(uuid.uuid4())
    if nav_id:
        headers["x-openai-document-navigation-id"] = nav_id
    return headers


def nav_headers_html(referer: str, fetch_site: str) -> dict[str, str]:
    return chrome_nav_headers(referer, fetch_site)


def json_headers(referer: str, origin: str, same_site: bool = False) -> dict[str, str]:
    return chrome_xhr_headers(
        referer,
        origin,
        accept="*/*",
        same_origin=not same_site,
    )
