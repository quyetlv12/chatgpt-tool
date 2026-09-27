"""RFC 6238 TOTP + base32 secret normalize."""

from __future__ import annotations

import base64

import pyotp

TIME_STEP = 30
DIGITS = 6


def normalize_secret(secret: str) -> str:
    cleaned = "".join(c for c in secret if not c.isspace() and c not in "-=").upper()
    if not cleaned:
        raise ValueError("totp secret is empty")
    pad = (-len(cleaned)) % 8
    try:
        base64.b32decode(cleaned + ("=" * pad), casefold=True)
    except Exception as exc:
        raise ValueError(f"invalid base32 secret: {exc}") from exc
    return cleaned


def generate_code(secret: str, at_unix: int | None = None) -> str:
    totp = pyotp.TOTP(normalize_secret(secret), digits=DIGITS, interval=TIME_STEP)
    if at_unix is None:
        return totp.now()
    return totp.at(at_unix)
