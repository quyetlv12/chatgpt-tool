"""Parse `email|password[|totp_secret]` lines."""

from __future__ import annotations

from dataclasses import dataclass

from gpt_tool.totp import normalize_secret


class ParseLineError(ValueError):
    pass


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str
    totp_secret: str | None = None


def normalize_email(email: str) -> str:
    return email.strip().lower()


def parse_line(raw: str) -> Credentials | None:
    trimmed = raw.strip()
    if not trimmed or trimmed.startswith("#"):
        return None

    parts = trimmed.split("|")
    if len(parts) == 2:
        email, password = parts
        email, password = email.strip(), password.strip()
        _check_non_empty(email, password)
        return Credentials(email=normalize_email(email), password=password, totp_secret=None)
    if len(parts) == 3:
        email, password, totp = parts
        email, password = email.strip(), password.strip()
        _check_non_empty(email, password)
        totp = totp.strip()
        if not totp:
            return Credentials(email=normalize_email(email), password=password, totp_secret=None)
        try:
            secret = normalize_secret(totp)
        except ValueError as exc:
            raise ParseLineError(str(exc)) from exc
        return Credentials(email=normalize_email(email), password=password, totp_secret=secret)
    raise ParseLineError("invalid format: expected email|password[|totp]")


def parse_lines(text: str) -> list[Credentials]:
    out: list[Credentials] = []
    for raw in text.splitlines():
        creds = parse_line(raw)
        if creds is not None:
            out.append(creds)
    return out


def _check_non_empty(email: str, password: str) -> None:
    if not email.strip():
        raise ParseLineError("empty email")
    if password == "":
        raise ParseLineError("empty password")
