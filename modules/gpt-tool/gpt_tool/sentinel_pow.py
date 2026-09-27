"""Pure-HTTP Sentinel PoW (port of rust-gpt-reg-checkout sentinel/pow.rs)."""

from __future__ import annotations

import base64
import json
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from gpt_tool.http_client import (
    ACCEPT_ENCODING,
    ACCEPT_LANGUAGE,
    LOGIN_UA,
    SEC_CH_UA,
)

SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
SENTINEL_SDK_VER = "20260810913b"
SENTINEL_REFERER = (
    f"https://sentinel.openai.com/backend-api/sentinel/frame.html?sv={SENTINEL_SDK_VER}"
)
SENTINEL_SDK_URL = f"https://sentinel.openai.com/sentinel/{SENTINEL_SDK_VER}/sdk.js"
ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"
MAX_ATTEMPTS = 500_000

FNV_OFFSET = 2_166_136_261
FNV_PRIME = 16_777_619
MIX1 = 2_246_822_507
MIX2 = 3_266_489_909

NAV_PROPS = [
    "vendorSub",
    "productSub",
    "vendor",
    "maxTouchPoints",
    "scheduling",
    "userActivation",
    "doNotTrack",
    "geolocation",
    "connection",
    "plugins",
    "mimeTypes",
    "pdfViewerEnabled",
    "webkitTemporaryStorage",
    "webkitPersistentStorage",
    "hardwareConcurrency",
    "cookieEnabled",
    "credentials",
    "mediaDevices",
    "permissions",
    "locks",
    "ink",
]
DOC_FIELDS = ["location", "implementation", "URL", "documentURI", "compatMode"]
GLOBAL_FIELDS = ["Object", "Function", "Array", "Number", "parseFloat", "undefined"]
HW_CONCURRENCY = [4, 8, 12, 16]


def fnv1a_32(text: str) -> str:
    h = FNV_OFFSET
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * FNV_PRIME) & 0xFFFFFFFF
    h ^= h >> 16
    h = (h * MIX1) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * MIX2) & 0xFFFFFFFF
    h ^= h >> 16
    return f"{h:08x}"


def b64_encode_json(value: Any) -> str:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def build_config(user_agent: str) -> list[Any]:
    now = datetime.now(tz=timezone.utc)
    date_str = now.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)")
    perf_now = random.uniform(1000.0, 50_000.0)
    time_origin = now.timestamp() * 1000 - perf_now
    return [
        "1920x1080",
        date_str,
        4_294_705_152,
        random.random(),
        user_agent,
        SENTINEL_SDK_URL,
        None,
        None,
        "vi-VN",
        "vi-VN,vi,fr-FR,fr,en-US,en",
        random.random(),
        f"{random.choice(NAV_PROPS)}−undefined",
        random.choice(DOC_FIELDS),
        random.choice(GLOBAL_FIELDS),
        perf_now,
        str(uuid.uuid4()),
        "",
        random.choice(HW_CONCURRENCY),
        time_origin,
    ]


def generate_requirements_token(user_agent: str) -> str:
    config = build_config(user_agent)
    config[3] = 1
    config[9] = random.randint(5, 49)
    return "gAAAAAC" + b64_encode_json(config)


def digest_prefix_le(digest: str, difficulty: str) -> bool:
    if not difficulty:
        return True
    if len(digest) < len(difficulty):
        return False
    return digest[: len(difficulty)] <= difficulty


def solve_pow(seed: str, difficulty: str, user_agent: str, max_attempts: int = MAX_ATTEMPTS) -> str:
    config = build_config(user_agent)
    started = time.monotonic()
    for nonce in range(max_attempts):
        config[3] = nonce
        config[9] = int((time.monotonic() - started) * 1000)
        encoded = b64_encode_json(config)
        digest = fnv1a_32(seed + encoded)
        if digest_prefix_le(digest, difficulty):
            return f"gAAAAAB{encoded}~S"
    return f"gAAAAAB{ERROR_PREFIX}{b64_encode_json('None')}"


def build_token(p: str, t: str, c: str, device_id: str, flow: str) -> str:
    return json.dumps(
        {"p": p, "t": t, "c": c, "id": device_id, "flow": flow},
        separators=(",", ":"),
        ensure_ascii=False,
    )


def get_sentinel_token_pow(session, device_id: str, flow: str = "password_verify", user_agent: str = LOGIN_UA) -> str:
    did = device_id or str(uuid.uuid4())
    request_p = generate_requirements_token(user_agent)
    try:
        resp = session.post(
            SENTINEL_REQ_URL,
            data=json.dumps({"p": request_p, "id": did, "flow": flow}, separators=(",", ":")),
            headers={
                "sec-ch-ua-platform": '"macOS"',
                "user-agent": user_agent,
                "sec-ch-ua": SEC_CH_UA,
                "content-type": "text/plain;charset=UTF-8",
                "sec-ch-ua-mobile": "?0",
                "accept": "*/*",
                "origin": "https://sentinel.openai.com",
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
                "referer": SENTINEL_REFERER,
                "accept-encoding": ACCEPT_ENCODING,
                "accept-language": ACCEPT_LANGUAGE,
                "priority": "u=1, i",
            },
            timeout=20,
        )
        if resp.status_code >= 400:
            return build_token(request_p, "", "", did, flow)
        data = resp.json()
        pow_info = data.get("proofofwork") or {}
        token_c = data.get("token") or ""
        if pow_info.get("required") and pow_info.get("seed"):
            solved = solve_pow(str(pow_info["seed"]), str(pow_info.get("difficulty") or "0"), user_agent)
            return build_token(solved, "", token_c, did, flow)
        return build_token(request_p, "", token_c, did, flow)
    except Exception:
        return build_token(request_p, "", "", did, flow)
