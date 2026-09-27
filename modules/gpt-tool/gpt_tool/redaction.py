"""Never log passwords or tokens."""

from __future__ import annotations

import re

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+")


def redact(text: str) -> str:
    out = _JWT.sub("eyJ…[redacted]", text)
    out = _BEARER.sub(r"\1[redacted]", out)
    return out


def short(text: str, n: int = 220) -> str:
    t = redact(text).replace("\n", " ")
    return t if len(t) <= n else t[: n - 1] + "…"
