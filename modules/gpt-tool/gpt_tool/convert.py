"""Bidirectional converter for GPTSession2CPA formats."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from gpt_tool.jwtutil import decode_payload, encode_b64url_json, openai_auth, openai_profile

FORMATS = (
    "cpa",
    "sub2api",
    "cockpit",
    "9router",
    "codex",
    "axonhub",
    "codexmanager",
)

AXONHUB_PLACEHOLDER = "__missing_refresh_token__"


def first_non_empty(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _plain(value: Any) -> bool:
    return isinstance(value, dict)


def collect_session_like(value: Any, source_name: str = "pasted-json") -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[int] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            oid = id(item)
            if oid in seen:
                return
            seen.add(oid)
            token = first_non_empty(
                item.get("accessToken"),
                item.get("access_token"),
                (item.get("tokens") or {}).get("accessToken") if _plain(item.get("tokens")) else None,
                (item.get("tokens") or {}).get("access_token") if _plain(item.get("tokens")) else None,
                (item.get("token") or {}).get("accessToken") if _plain(item.get("token")) else None,
                (item.get("token") or {}).get("access_token") if _plain(item.get("token")) else None,
                (item.get("credentials") or {}).get("accessToken") if _plain(item.get("credentials")) else None,
                (item.get("credentials") or {}).get("access_token") if _plain(item.get("credentials")) else None,
            )
            tokens = item.get("tokens") if _plain(item.get("tokens")) else {}
            identity = (
                _plain(item.get("user"))
                or item.get("auth_mode") == "chatgpt"
                or first_non_empty(
                    item.get("email"),
                    item.get("name"),
                    item.get("label"),
                    (item.get("meta") or {}).get("label") if _plain(item.get("meta")) else None,
                    tokens.get("account_id"),
                    tokens.get("accountId"),
                    tokens.get("id_token"),
                    tokens.get("refresh_token"),
                    (item.get("providerSpecificData") or {}).get("chatgptAccountId")
                    if _plain(item.get("providerSpecificData"))
                    else None,
                    item.get("id"),
                    item.get("type"),
                )
            )
            if token and identity:
                found.append(item)
                return
            for key, child in item.items():
                if key in {"accessToken", "access_token", "sessionToken"}:
                    continue
                visit(child)
            return
        if isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def parse_input_documents(text: str) -> list[dict[str, Any]]:
    if not text or not text.strip():
        return []
    parsed = json.loads(text)
    return collect_session_like(parsed)


def _nested(record: dict[str, Any], *path: str) -> Any:
    cur: Any = record
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)):
        ms = value if value > 1e11 else value * 1000
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            return None
    return None


def _unix_exp(payload: dict[str, Any]) -> int | None:
    exp = payload.get("exp")
    try:
        n = int(exp)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _epoch_seconds(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        n = int(value)
        return n // 1000 if n > 1e11 else n
    if isinstance(value, str):
        iso = _iso(value)
        if not iso:
            return 0
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp())
    return 0


def build_synthetic_id_token(
    email: str | None,
    account_id: str | None,
    plan_type: str | None,
    user_id: str | None,
    expires_at: str | None,
) -> str | None:
    if not account_id:
        return None
    now = int(datetime.now(tz=timezone.utc).timestamp())
    auth: dict[str, Any] = {"chatgpt_account_id": account_id}
    expires = _epoch_seconds(expires_at) or now + 90 * 24 * 60 * 60
    if plan_type:
        auth["chatgpt_plan_type"] = plan_type
    if user_id:
        auth["chatgpt_user_id"] = user_id
        auth["user_id"] = user_id
    payload: dict[str, Any] = {
        "iat": now,
        "exp": expires,
        "https://api.openai.com/auth": auth,
    }
    if email:
        payload["email"] = email
    header = {"alg": "none", "typ": "JWT", "cpa_synthetic": True}
    return f"{encode_b64url_json(header)}.{encode_b64url_json(payload)}.synthetic"


def strip_unavailable(value: Any) -> Any:
    if isinstance(value, list):
        return [item for item in (strip_unavailable(v) for v in value) if item is not None]
    if isinstance(value, dict):
        out = {k: strip_unavailable(v) for k, v in value.items()}
        out = {k: v for k, v in out.items() if v is not None}
        return out or None
    if value in (None, ""):
        return None
    return value


def to_email_key(email: str | None) -> str | None:
    if not isinstance(email, str):
        return None
    key = re.sub(r"[^a-z0-9]+", "_", email.strip().lower()).strip("_")
    return key or None


def sanitize_file_token(value: str | None, fallback: str = "chatgpt-session") -> str:
    base = first_non_empty(value, fallback) or fallback
    if re.search(r"\.(json|txt|html)$", base, flags=re.I):
        base = re.sub(r"\.[^.]+$", "", base)
    base = re.sub(r'[\\/:*?"<>|]+', "-", base)
    base = re.sub(r"\s+", "-", base)
    base = re.sub(r"-+", "-", base).strip("-").lower()[:80]
    return base or fallback


def convert_record(record: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("session is not a JSON object")

    access_token = first_non_empty(
        record.get("accessToken"),
        record.get("access_token"),
        _nested(record, "tokens", "accessToken"),
        _nested(record, "tokens", "access_token"),
        _nested(record, "token", "accessToken"),
        _nested(record, "token", "access_token"),
        _nested(record, "credentials", "accessToken"),
        _nested(record, "credentials", "access_token"),
    )
    if not access_token:
        raise ValueError("missing accessToken")

    session_token = first_non_empty(
        record.get("sessionToken"),
        record.get("session_token"),
        _nested(record, "tokens", "sessionToken"),
        _nested(record, "tokens", "session_token"),
        _nested(record, "credentials", "session_token"),
    )
    refresh_token = first_non_empty(
        record.get("refreshToken"),
        record.get("refresh_token"),
        _nested(record, "tokens", "refreshToken"),
        _nested(record, "tokens", "refresh_token"),
        _nested(record, "token", "refreshToken"),
        _nested(record, "token", "refresh_token"),
        _nested(record, "credentials", "refresh_token"),
    )
    input_id_token = first_non_empty(
        record.get("idToken"),
        record.get("id_token"),
        _nested(record, "tokens", "idToken"),
        _nested(record, "tokens", "id_token"),
        _nested(record, "token", "idToken"),
        _nested(record, "token", "id_token"),
        _nested(record, "credentials", "id_token"),
    )

    payload = decode_payload(access_token)
    id_payload = decode_payload(input_id_token)
    auth = openai_auth(payload)
    id_auth = openai_auth(id_payload)
    profile = openai_profile(payload)
    has_refresh = bool(refresh_token)
    access_token_expires_at = None if has_refresh else _unix_exp(payload)
    expires_at = None
    if not has_refresh:
        expires_at = first_non_empty(
            _iso(payload.get("exp")) if payload.get("exp") is not None else None,
            _iso(record.get("expires")),
            _iso(record.get("expiresAt")),
            _iso(record.get("expired")),
            _iso(record.get("expires_at")),
        )

    email = first_non_empty(
        _nested(record, "user", "email"),
        record.get("email"),
        _nested(record, "meta", "label"),
        record.get("label"),
        _nested(record, "credentials", "email"),
        _nested(record, "providerSpecificData", "email"),
        profile.get("email") if isinstance(profile.get("email"), str) else None,
        id_payload.get("email") if isinstance(id_payload.get("email"), str) else None,
        payload.get("email") if isinstance(payload.get("email"), str) else None,
    )
    account_id = first_non_empty(
        _nested(record, "account", "id"),
        record.get("account_id"),
        _nested(record, "tokens", "accountId"),
        _nested(record, "tokens", "account_id"),
        record.get("chatgptAccountId"),
        record.get("chatgpt_account_id"),
        _nested(record, "meta", "chatgptAccountId"),
        _nested(record, "meta", "chatgpt_account_id"),
        _nested(record, "tokens", "chatgptAccountId"),
        _nested(record, "tokens", "chatgpt_account_id"),
        _nested(record, "providerSpecificData", "chatgptAccountId"),
        _nested(record, "providerSpecificData", "chatgpt_account_id"),
        _nested(record, "credentials", "chatgpt_account_id"),
        auth.get("chatgpt_account_id") if isinstance(auth.get("chatgpt_account_id"), str) else None,
        id_auth.get("chatgpt_account_id") if isinstance(id_auth.get("chatgpt_account_id"), str) else None,
        record.get("id") if record.get("provider") == "codex" else None,
    )
    chatgpt_account_id = first_non_empty(
        record.get("chatgptAccountId"),
        record.get("chatgpt_account_id"),
        _nested(record, "meta", "chatgptAccountId"),
        _nested(record, "meta", "chatgpt_account_id"),
        _nested(record, "tokens", "chatgptAccountId"),
        _nested(record, "tokens", "chatgpt_account_id"),
        _nested(record, "providerSpecificData", "chatgptAccountId"),
        _nested(record, "providerSpecificData", "chatgpt_account_id"),
        _nested(record, "credentials", "chatgpt_account_id"),
        auth.get("chatgpt_account_id") if isinstance(auth.get("chatgpt_account_id"), str) else None,
        id_auth.get("chatgpt_account_id") if isinstance(id_auth.get("chatgpt_account_id"), str) else None,
    )
    user_id = first_non_empty(
        _nested(record, "user", "id"),
        record.get("user_id"),
        record.get("chatgptUserId"),
        _nested(record, "providerSpecificData", "chatgptUserId"),
        _nested(record, "providerSpecificData", "chatgpt_user_id"),
        auth.get("chatgpt_user_id") if isinstance(auth.get("chatgpt_user_id"), str) else None,
        auth.get("user_id") if isinstance(auth.get("user_id"), str) else None,
        id_auth.get("chatgpt_user_id") if isinstance(id_auth.get("chatgpt_user_id"), str) else None,
        id_auth.get("user_id") if isinstance(id_auth.get("user_id"), str) else None,
    )
    plan_type = first_non_empty(
        _nested(record, "account", "planType"),
        _nested(record, "account", "plan_type"),
        record.get("planType"),
        record.get("plan_type"),
        _nested(record, "providerSpecificData", "chatgptPlanType"),
        _nested(record, "providerSpecificData", "chatgpt_plan_type"),
        _nested(record, "credentials", "plan_type"),
        auth.get("chatgpt_plan_type") if isinstance(auth.get("chatgpt_plan_type"), str) else None,
        id_auth.get("chatgpt_plan_type") if isinstance(id_auth.get("chatgpt_plan_type"), str) else None,
    )
    workspace_id = first_non_empty(
        _nested(record, "account", "workspaceId"),
        _nested(record, "account", "workspace_id"),
        record.get("workspaceId"),
        record.get("workspace_id"),
        _nested(record, "meta", "workspaceId"),
        _nested(record, "meta", "workspace_id"),
        _nested(record, "providerSpecificData", "workspaceId"),
        _nested(record, "credentials", "workspace_id"),
    )

    exported_at = _iso(now or datetime.now(tz=timezone.utc))
    expires_in = None
    if expires_at:
        try:
            exp_ms = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp() * 1000
            expires_in = max(0, int((exp_ms - datetime.now(tz=timezone.utc).timestamp() * 1000) / 1000))
        except ValueError:
            expires_in = None

    name = first_non_empty(email, "ChatGPT Account")
    synthetic = None if input_id_token else build_synthetic_id_token(
        email, account_id, plan_type, user_id, expires_at
    )
    id_token = first_non_empty(input_id_token, synthetic)
    source_type = (
        "9router"
        if record.get("provider") == "codex" and record.get("authType") == "oauth"
        else "chatgpt_web_session"
    )

    cpa = {
        k: v
        for k, v in {
            "type": "codex",
            "account_id": account_id,
            "chatgpt_account_id": account_id,
            "email": email,
            "name": name,
            "plan_type": plan_type,
            "chatgpt_plan_type": plan_type,
            "id_token": id_token,
            "id_token_synthetic": True if synthetic else None,
            "access_token": access_token,
            "refresh_token": refresh_token or "",
            "session_token": session_token,
            "last_refresh": exported_at,
            "expired": expires_at,
            "disabled": True if record.get("disabled") else None,
        }.items()
        if v is not None
    }

    cockpit = {
        "type": "codex",
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token or "",
        "account_id": account_id,
        "last_refresh": exported_at,
        "email": email,
        "expired": expires_at,
        "account_note": first_non_empty(
            record.get("account_note"),
            record.get("accountInfo"),
            record.get("note"),
            record.get("notes"),
        ),
    }

    sub2api_account = strip_unavailable(
        {
            "name": first_non_empty(name, email, "ChatGPT Account"),
            "platform": "openai",
            "type": "oauth",
            "expires_at": access_token_expires_at,
            "auto_pause_on_expired": True if access_token_expires_at else None,
            "concurrency": 10,
            "priority": 1,
            "credentials": {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "chatgpt_account_id": account_id,
                "chatgpt_user_id": user_id,
                "email": email,
                "expires_at": expires_at,
                "expires_in": expires_in,
                "plan_type": plan_type,
            },
            "extra": {
                "email": email,
                "email_key": to_email_key(email),
                "name": name,
                "auth_provider": first_non_empty(record.get("authProvider"), record.get("auth_provider")),
                "source": source_type,
                "last_refresh": exported_at,
            },
        }
    )

    try:
        priority = int(record.get("priority", 9))
    except (TypeError, ValueError):
        priority = 9
    is_active = record["isActive"] if isinstance(record.get("isActive"), bool) else not bool(record.get("disabled"))
    nine_router = strip_unavailable(
        {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "idToken": id_token,
            "expiresAt": expires_at,
            "testStatus": first_non_empty(record.get("testStatus"), record.get("test_status"), "active"),
            "expiresIn": expires_in,
            "providerSpecificData": {
                "chatgptAccountId": account_id,
                "chatgptPlanType": plan_type,
            },
            "id": account_id,
            "provider": "codex",
            "authType": "oauth",
            "name": name,
            "email": email,
            "priority": priority,
            "isActive": is_active,
            "createdAt": _iso(record.get("createdAt")) or exported_at,
            "updatedAt": _iso(record.get("updatedAt")) or exported_at,
        }
    )

    axon_refresh = refresh_token or AXONHUB_PLACEHOLDER
    axon_last = exported_at
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            axon_last = datetime.fromtimestamp(exp_dt.timestamp() - 3600, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass

    axonhub = strip_unavailable(
        {
            "auth_mode": "chatgpt",
            "last_refresh": axon_last,
            "tokens": {
                "access_token": access_token,
                "refresh_token": axon_refresh,
                "id_token": id_token,
            },
            "axonhub_refresh_token_placeholder": None if refresh_token else True,
            "axonhub_note": None
            if refresh_token
            else "refresh_token is a placeholder; access_token works only until it expires.",
        }
    )

    token_hints = {
        k: v
        for k, v in {
            "account_id": account_id,
            "chatgpt_account_id": chatgpt_account_id,
        }.items()
        if v
    }
    codex_manager = {
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token or "",
            "id_token": input_id_token or "",
            **token_hints,
        },
        "meta": {
            k: v
            for k, v in {
                "label": first_non_empty(name, email, "ChatGPT Account"),
                "workspace_id": workspace_id,
                "chatgpt_account_id": chatgpt_account_id,
                "note": "Imported from ChatGPT session",
            }.items()
            if v
        },
    }

    return {
        "email": email,
        "name": name,
        "expiresAt": expires_at,
        "cpa": cpa,
        "cockpit": cockpit,
        "nineRouter": nine_router,
        "codexAuthJson": {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "id_token": id_token,
                "access_token": access_token,
                "refresh_token": refresh_token or "",
                "account_id": account_id,
            },
            "last_refresh": exported_at,
        },
        "axonHub": axonhub,
        "codexManager": codex_manager,
        "sub2apiAccount": sub2api_account,
    }


def emit_format(converted: dict[str, Any], fmt: str, now: datetime | None = None) -> Any:
    fmt = fmt.lower().strip()
    if fmt not in FORMATS:
        raise ValueError(f"unknown format: {fmt}")
    if fmt == "cpa":
        return converted["cpa"]
    if fmt == "cockpit":
        return converted["cockpit"]
    if fmt == "9router":
        return converted["nineRouter"]
    if fmt == "codex":
        return converted["codexAuthJson"]
    if fmt == "axonhub":
        return converted["axonHub"]
    if fmt == "codexmanager":
        return converted["codexManager"]
    return {
        "exported_at": _iso(now or datetime.now(tz=timezone.utc)),
        "proxies": [],
        "accounts": [converted["sub2apiAccount"]],
    }


def convert_text(text: str, fmt: str) -> list[tuple[str, Any]]:
    sources = parse_input_documents(text)
    if not sources:
        raise ValueError("no session object with accessToken + identity found")
    now = datetime.now(tz=timezone.utc)
    out: list[tuple[str, Any]] = []
    for record in sources:
        converted = convert_record(record, now=now)
        email = converted.get("email") or "account"
        out.append((str(email), emit_format(converted, fmt, now=now)))
    return out


def canonical_from_oauth(
    *,
    email: str,
    access_token: str,
    refresh_token: str,
    id_token: str | None,
    account_id: str | None,
    session_token: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "codex",
        "email": email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token or "",
        "account_id": account_id or "",
        "session_token": session_token or "",
    }


def convert_canonical(canonical: dict[str, Any], fmt: str) -> Any:
    converted = convert_record(canonical)
    return emit_format(converted, fmt)
