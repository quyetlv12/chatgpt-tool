from __future__ import annotations

import base64
import re
from dataclasses import dataclass


class CredentialError(ValueError):
    pass


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str
    totp_secret: str | None


def normalize_totp_secret(value: str) -> str:
    secret = re.sub(r"[\s-]+", "", value).upper()
    if not secret:
        raise CredentialError("2FA secret trống")
    try:
        base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8), casefold=True)
    except Exception as exc:
        raise CredentialError("2FA secret không hợp lệ") from exc
    return secret


def parse_credentials(raw: str) -> Credentials:
    if not isinstance(raw, str) or "\n" in raw or "\r" in raw:
        raise CredentialError("Mỗi tài khoản phải nằm trên đúng một dòng")
    parts = raw.strip().split("|")
    if len(parts) not in (2, 3):
        raise CredentialError("Định dạng phải là email|mật khẩu|2fa")
    email, password = parts[0].strip().lower(), parts[1].strip()
    if not email or "@" not in email:
        raise CredentialError("Email không hợp lệ")
    if not password:
        raise CredentialError("Mật khẩu không được để trống")
    secret = parts[2].strip() if len(parts) == 3 else ""
    return Credentials(email, password, normalize_totp_secret(secret) if secret else None)


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}***@{domain}"
