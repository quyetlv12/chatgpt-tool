"""Decode JWT payloads without verifying signatures."""

from __future__ import annotations

import base64
import json
from typing import Any


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * ((4 - len(segment) % 4) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def decode_payload(token: str | None) -> dict[str, Any]:
    if not token or not isinstance(token, str) or token.count(".") < 1:
        return {}
    try:
        payload = token.split(".")[1]
        data = json.loads(_b64url_decode(payload))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def openai_auth(payload: dict[str, Any]) -> dict[str, Any]:
    auth = payload.get("https://api.openai.com/auth") or payload.get("auth") or {}
    return auth if isinstance(auth, dict) else {}


def openai_profile(payload: dict[str, Any]) -> dict[str, Any]:
    profile = payload.get("https://api.openai.com/profile") or payload.get("profile") or {}
    return profile if isinstance(profile, dict) else {}


def encode_b64url_json(value: Any) -> str:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
