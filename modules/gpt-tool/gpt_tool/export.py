"""Login → Codex OAuth → refresh → one JSON file per account."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

from gpt_tool.convert import FORMATS, canonical_from_oauth, convert_canonical, convert_text, sanitize_file_token
from gpt_tool.login import LoginError, login_keep_session
from gpt_tool.oauth import AddPhoneRequired, OAuthError, account_id_from_access, oauth_codex_rt_exchange, refresh_codex_token
from gpt_tool.parser import ParseLineError, parse_line
from gpt_tool.redaction import redact

DEFAULT_OUT = Path("out")


@dataclass
class Outcome:
    email: str
    ok: bool
    path: str | None = None
    error: str | None = None
    step: str | None = None
    index: int | None = None
    cockpit_payload: object | None = None
    nine_router_payload: object | None = None
    cancelled: bool = False


def write_json(out_dir: Path, email: str, body: object) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = sanitize_file_token(email.replace("@", "_at_"), "account") + ".json"
    dest = out_dir / name
    n = 2
    while dest.exists():
        dest = out_dir / f"{sanitize_file_token(email.replace('@', '_at_'), 'account')}_{n}.json"
        n += 1
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(dest)
    return dest


def append_failed(out_dir: Path, email: str, step: str, message: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    line = f"{email}|{step}|{redact(message)}\n"
    with (out_dir / "failed.txt").open("a", encoding="utf-8") as fh:
        fh.write(line)


def peek_email(raw_line: str) -> str:
    return (raw_line.split("|")[0] if raw_line else "").strip().lower()


def export_one(
    raw_line: str,
    fmt: str,
    out_dir: Path,
    proxy: str | None = None,
    on_step=None,
    index: int | None = None,
    cockpit_import: bool = False,
    nine_router_import: bool = False,
    cancel_event=None,
) -> Outcome:
    def step(email: str, name: str) -> None:
        if on_step:
            on_step(email, name, index)

    def done(email: str, ok: bool, **kwargs) -> Outcome:
        return Outcome(email=email, ok=ok, index=index, **kwargs)

    def cancelled(email: str) -> Outcome:
        return done(email, False, error="Đã dừng", step="cancelled", cancelled=True)

    fmt = fmt.lower().strip()
    if fmt not in FORMATS:
        return done("", False, error=f"unknown format: {fmt}", step="parse")
    email = peek_email(raw_line)
    if cancel_event is not None and cancel_event.is_set():
        return cancelled(email)
    step(email, "parse")
    try:
        creds = parse_line(raw_line)
    except ParseLineError as exc:
        append_failed(out_dir, email, "parse", str(exc))
        return done(email, False, error=str(exc), step="parse")
    if creds is None:
        return done(email, False, error="empty or comment line", step="parse")
    email = creds.email
    if cancel_event is not None and cancel_event.is_set():
        return cancelled(email)
    step(email, "oauth")
    try:
        bundle, session = login_keep_session(creds, proxy, cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            return cancelled(email)
        tokens = oauth_codex_rt_exchange(session, bundle.device_id, creds)
    except InterruptedError:
        return cancelled(email)
    except LoginError as exc:
        append_failed(out_dir, email, "login", str(exc))
        return done(email, False, error=str(exc), step="login")
    except AddPhoneRequired as exc:
        append_failed(out_dir, email, "oauth", str(exc))
        return done(email, False, error=str(exc), step="oauth")
    except OAuthError as exc:
        append_failed(out_dir, email, "oauth", str(exc))
        return done(email, False, error=str(exc), step="oauth")
    rt = tokens.refresh_token or ""
    if cancel_event is not None and cancel_event.is_set():
        return cancelled(email)
    step(email, "refresh")
    try:
        refreshed = refresh_codex_token(rt, proxy, cancel_event=cancel_event)
    except InterruptedError:
        return cancelled(email)
    except OAuthError as exc:
        append_failed(out_dir, email, "refresh", str(exc))
        return done(email, False, error=str(exc), step="refresh")
    step(email, "export")
    if cancel_event is not None and cancel_event.is_set():
        return cancelled(email)
    try:
        access = refreshed.access_token or ""
        rt_final = refreshed.refresh_token or rt
        id_token = refreshed.id_token or tokens.id_token
        canonical = canonical_from_oauth(
            email=email,
            access_token=access,
            refresh_token=rt_final,
            id_token=id_token,
            account_id=account_id_from_access(access),
            session_token=bundle.session_token,
        )
        body = convert_canonical(canonical, fmt)
        path = write_json(out_dir, email, body)
        cockpit_payload = convert_canonical(canonical, "cockpit") if cockpit_import else None
        nine_router_payload = convert_canonical(canonical, "9router") if nine_router_import else None
    except Exception as exc:
        append_failed(out_dir, email, "export", str(exc))
        return done(email, False, error=str(exc), step="export")
    return done(
        email,
        True,
        path=str(path),
        step="done",
        cockpit_payload=cockpit_payload,
        nine_router_payload=nine_router_payload,
    )


def export_bulk(
    lines: list[str],
    fmt: str,
    out_dir: Path | None = None,
    proxy: str | None = None,
    workers: int = 2,
    on_progress=None,
    on_step=None,
    cockpit_import: bool = False,
    nine_router_import: bool = False,
    cancel_event=None,
) -> list[Outcome]:
    out = out_dir or DEFAULT_OUT
    workers = max(1, min(8, int(workers or 2)))
    jobs = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    results: list[Outcome] = []

    def run_line(index: int, line: str) -> Outcome:
        email = peek_email(line)
        if cancel_event is not None and cancel_event.is_set():
            return Outcome(
                email=email,
                ok=False,
                error="Đã dừng",
                step="cancelled",
                index=index,
                cancelled=True,
            )
        if on_step:
            on_step(email, "queued", index)
        return export_one(
            line,
            fmt,
            out,
            proxy,
            on_step=on_step,
            index=index,
            cockpit_import=cockpit_import,
            nine_router_import=nine_router_import,
            cancel_event=cancel_event,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(run_line, index, line): (index, line) for index, line in enumerate(jobs)}
        for fut in as_completed(futs):
            index, line = futs[fut]
            try:
                outcome = fut.result()
            except Exception as exc:
                outcome = Outcome(email=peek_email(line), ok=False, error=str(exc), step="export", index=index)
            if outcome.index is None:
                outcome.index = index
            results.append(outcome)
            if on_progress:
                on_progress(outcome)
    return results


def convert_bulk(text: str, fmt: str, out_dir: Path | None = None) -> list[Outcome]:
    out = out_dir or DEFAULT_OUT
    pairs = convert_text(text, fmt)
    results: list[Outcome] = []
    for email, body in pairs:
        path = write_json(out, email, body)
        results.append(Outcome(email=email, ok=True, path=str(path), step="done"))
    return results


def outcome_dict(o: Outcome) -> dict:
    return asdict(o)
