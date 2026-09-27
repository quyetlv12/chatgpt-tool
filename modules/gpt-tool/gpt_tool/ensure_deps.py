"""Install/upgrade runtime libs so Chrome impersonate matches this curl_cffi."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

NEED = ("curl_cffi==0.15.0", "pyotp>=2.9.0,<3")
CHROME_OK = ("chrome142", "chrome136", "chrome133a", "chrome131", "chrome124", "chrome120")


def _pip(*args: str) -> None:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-U", *args], stdout=subprocess.DEVNULL)


def _specs() -> list[str]:
    req = Path(__file__).resolve().parent.parent / "requirements.txt"
    if not req.is_file():
        return list(NEED)
    specs = []
    for line in req.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or raw.startswith("pytest"):
            continue
        specs.append(raw)
    return specs or list(NEED)


def chrome_supported() -> bool:
    try:
        from curl_cffi.requests.impersonate import BrowserType

        names = {str(item.value) for item in BrowserType}
        return any(name in names for name in CHROME_OK)
    except Exception:
        return False


def ensure_deps() -> None:
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return
    missing = False
    try:
        import curl_cffi  # noqa: F401
        import pyotp  # noqa: F401
    except Exception:
        missing = True
    if not missing and chrome_supported():
        return
    print("Đang tải thư viện (curl_cffi)… lần đầu có thể mất 1–2 phút.", flush=True)
    _pip(*_specs())
    if chrome_supported():
        return
    _pip("--force-reinstall", "curl_cffi==0.15.0")
    if not chrome_supported():
        print("Cảnh báo: curl_cffi không có Chrome impersonate. Thử: pip install -U 'curl_cffi==0.15.0'", flush=True)
