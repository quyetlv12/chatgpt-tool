"""Local FastAPI control plane for Shoptaikhoan Tool."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import certifi
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr


APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
LEGACY_RUNTIME_DIR = APP_DIR / "runtime"


def resolve_runtime_dir(
    platform: str | None = None,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the native per-user data directory for the current platform."""
    platform_name = platform or sys.platform
    environment = os.environ if environ is None else environ
    user_home = Path.home() if home is None else home
    if platform_name.startswith("win"):
        base = Path(environment.get("LOCALAPPDATA", user_home / "AppData" / "Local"))
    elif platform_name == "darwin":
        base = user_home / "Library" / "Application Support"
    else:
        base = Path(environment.get("XDG_DATA_HOME", user_home / ".local" / "share"))
    return base / "InfinityAIStore" / "Change2FA"


RUNTIME_DIR = Path(os.environ.get("SHOPTAIKHOAN_TWOFA_DATA_DIR") or resolve_runtime_dir()).expanduser()
DB_PATH = RUNTIME_DIR / "twofa.db"
RUNTIME_PORT = 5033
RUNTIME_HOST = "127.0.0.1"


def _migrate_legacy_database() -> None:
    """Copy the source-mode database once; never bundle it into a release."""
    legacy = LEGACY_RUNTIME_DIR / "twofa.db"
    if DB_PATH.exists() or not legacy.is_file() or legacy.resolve() == DB_PATH.resolve():
        return
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{legacy.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(DB_PATH)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _prepare_runtime() -> None:
    """Keep Python I/O and TLS certificate paths safe in frozen builds."""
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    source = Path(certifi.where())
    if not source.is_file():
        raise RuntimeError(f"Không tìm thấy CA bundle: {source}")
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    target = RUNTIME_DIR / "cacert.pem"
    if not target.exists() or source.read_bytes() != target.read_bytes():
        shutil.copy2(source, target)
    for key in ("CURL_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        os.environ[key] = str(target)


_prepare_runtime()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from db import get_engine, get_repos, get_settings_repo  # noqa: E402
from db.mysql_sync import MySQLSync  # noqa: E402
from db.repositories import LiveJournalRepository  # noqa: E402
from jobs import TwoFAJobManager  # noqa: E402
from macos_integration import launch_menu_bar, menu_bar_command  # noqa: E402
from passkey_jobs import PasskeyJobManager  # noqa: E402
from password_jobs import PasswordJobManager  # noqa: E402
from service import TwoFAFlowError  # noqa: E402
from passkey_service import PasskeyHandoffs  # noqa: E402
from passkey_windows import PasskeyWindows  # noqa: E402

passkey_windows = PasskeyWindows()


RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
_migrate_legacy_database()


class BatchRequest(BaseModel):
    lines: list[str] = Field(min_length=1, max_length=500)
    mode: str = Field(pattern="^(check_only|change_2fa)$")


class LiveJournalRequest(BaseModel):
    name: str = Field(default="", max_length=100)


class SettingsRequest(BaseModel):
    max_concurrent: int = Field(ge=1, le=10)
    job_timeout: float = Field(ge=30, le=600)
    auto_retry: bool
    auto_retry_max: int = Field(ge=0, le=5)
    auto_retry_delay: float = Field(ge=0, le=60)
    change_enabled: bool
    read_usage: bool = True
    read_payment_methods: bool = True
    input_draft: str = Field(max_length=1_000_000)


class PasswordBatchRequest(BaseModel):
    lines: list[str] = Field(min_length=1, max_length=500)


class PasskeyBatchRequest(BaseModel):
    lines: list[str] = Field(min_length=1, max_length=500)


class DeleteChatsRequest(BaseModel):
    confirm: Literal["DELETE_ALL_CHATS"]


class LogoutSessionsRequest(BaseModel):
    confirm: Literal["LOGOUT_ALL_SESSIONS"]


class PasskeyRequest(BaseModel):
    confirm: Literal["ADD_PASSKEY"]


class PasskeyLaunchRequest(BaseModel):
    confirm: Literal["LAUNCH_PASSKEY"]
    native_window: bool = False


class PasswordSettingsRequest(BaseModel):
    # Length/content validation is deliberately performed after SecretStr has
    # hidden the input. Pydantic constraint errors include the raw rejected
    # value in their default 422 response.
    target_password: SecretStr


ExportView = Literal["all", "running", "success", "usage-low", "usage-full", "free-no-usage", "error"]
EXPORT_FILENAMES: dict[str, str] = {
    "all": "twofa-all.txt",
    "running": "twofa-running.txt",
    "success": "twofa-success-tab.txt",
    "usage-low": "twofa-usage-under-50.txt",
    "usage-full": "twofa-usage-100.txt",
    "free-no-usage": "twofa-free-plus-no-usage.txt",
    "error": "twofa-errors.txt",
}


engine = get_engine(str(DB_PATH))
_, job_repo, _ = get_repos(engine)
settings_repo = get_settings_repo(engine)
live_journals = LiveJournalRepository(engine)
database_sync = MySQLSync(engine, RUNTIME_DIR / "mysql-sync.json")
auth_token = settings_repo.get("web.auth_token")
if not isinstance(auth_token, str) or len(auth_token) < 32:
    auth_token = secrets.token_urlsafe(32)
    settings_repo.set("web.auth_token", auth_token)
manager = TwoFAJobManager(job_repo, settings_repo)
password_manager = PasswordJobManager(job_repo, settings_repo)
passkey_manager = PasskeyJobManager(
    max_concurrent=int(manager.settings["twofa.max_concurrent"]),
    job_timeout=float(manager.settings["twofa.job_timeout"]),
)
passkey_handoffs = PasskeyHandoffs()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await asyncio.to_thread(database_sync.start)
    try:
        manager.start()
        password_manager.start()
        passkey_manager.start()
        yield
    finally:
        try:
            await passkey_manager.shutdown()
            await password_manager.shutdown()
            await manager.shutdown()
        finally:
            await asyncio.to_thread(database_sync.stop)
            engine.close()


app = FastAPI(
    title="Shoptaikhoan Tool — Change 2FA",
    description="Local-only TOTP rotation control plane",
    version="1.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def disable_passkey_ui_cache(request: Request, call_next):
    """Keep an already-open admin page from reusing stale dashboard assets."""
    response = await call_next(request)
    if request.url.path in {"/", "/passkey"} or request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


def require_token(x_auth_token: str | None = Header(default=None)) -> None:
    if not x_auth_token or not secrets.compare_digest(x_auth_token, auth_token):
        raise HTTPException(status_code=401, detail="Token không hợp lệ")


def assert_no_bulk_passkey(email: str) -> None:
    if passkey_manager.is_busy(email):
        raise ValueError("Tài khoản đang thêm passkey hàng loạt; vui lòng chờ")


def assert_no_bulk_passkey_for_job(job_manager: Any, job_id: str) -> None:
    job = getattr(job_manager, "jobs", {}).get(job_id)
    if job is not None:
        assert_no_bulk_passkey(job.email)


@app.get("/api/bootstrap")
async def bootstrap() -> dict[str, Any]:
    return {
        "brand": "Shoptaikhoan",
        "product": "Shoptaikhoan Tool",
        "token": auth_token,
        "jobs": manager.snapshots(),
        "settings": manager.settings,
        "worker_health": manager.worker_health(),
    }


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "port": RUNTIME_PORT,
        "worker_health": manager.worker_health(),
        "password_worker_health": password_manager.worker_health(),
        "passkey_worker_health": passkey_manager.worker_health(),
    }


@app.get("/api/database-sync", dependencies=[Depends(require_token)])
async def database_sync_status() -> JSONResponse:
    return JSONResponse(
        await asyncio.to_thread(database_sync.status),
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/live-journals", dependencies=[Depends(require_token)])
async def save_live_journal(request: LiveJournalRequest) -> JSONResponse:
    entries = manager.live_journal_entries()
    try:
        journal = await asyncio.to_thread(live_journals.create, request.name, entries)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Chưa lưu được nhật ký vào SQLite. Danh sách hiện tại vẫn được giữ.") from exc
    return JSONResponse({"journal": journal}, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/api/live-journals", dependencies=[Depends(require_token)])
async def list_live_journals() -> JSONResponse:
    return JSONResponse({"journals": await asyncio.to_thread(live_journals.list)},
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/api/live-journals/{journal_id}", dependencies=[Depends(require_token)])
async def view_live_journal(journal_id: str) -> JSONResponse:
    try:
        data = await asyncio.to_thread(live_journals.get, journal_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhật ký") from exc
    return JSONResponse(data, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/api/live-journals/{journal_id}/accounts/{job_id}/raw", dependencies=[Depends(require_token)])
async def raw_live_journal_account(journal_id: str, job_id: str) -> PlainTextResponse:
    try:
        raw = await asyncio.to_thread(live_journals.raw, journal_id, job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản trong nhật ký") from exc
    return PlainTextResponse(raw, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.post("/api/jobs", dependencies=[Depends(require_token)])
async def add_jobs(request: BatchRequest) -> dict[str, Any]:
    try:
        for line in request.lines:
            email = line.split("|", 1)[0]
            if passkey_manager.is_busy(email):
                raise ValueError("Tài khoản đang thêm passkey hàng loạt; vui lòng chờ")
            if any(
                other.email.strip().casefold() == email.strip().casefold()
                and other.status in {"queued", "running"}
                for other in getattr(manager, "jobs", {}).values()
            ):
                raise ValueError("Tài khoản đang có thao tác 2FA; vui lòng chờ")
        return {"jobs": manager.add(request.lines, request.mode)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/jobs/export", dependencies=[Depends(require_token)])
async def export_jobs(view: ExportView = "all") -> PlainTextResponse:
    lines = manager.export_filtered(view)
    body = "\n".join(lines)
    if body:
        body += "\n"
    return PlainTextResponse(
        body,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{EXPORT_FILENAMES[view]}"',
            "X-Content-Type-Options": "nosniff",
            "X-Export-Count": str(len(lines)),
        },
    )


@app.get("/api/jobs/{job_id}/raw", dependencies=[Depends(require_token)])
async def raw_job(job_id: str) -> PlainTextResponse:
    try:
        body = manager.raw_combo(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job") from exc
    return PlainTextResponse(
        body,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/twofa-history", dependencies=[Depends(require_token)])
async def twofa_history() -> JSONResponse:
    history = manager.twofa_history()
    return JSONResponse(
        {"count": len(history), "history": history},
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/jobs/{job_id}/change-2fa", dependencies=[Depends(require_token)])
async def change_job_2fa(job_id: str) -> dict[str, Any]:
    try:
        assert_no_bulk_passkey_for_job(manager, job_id)
        return {"job": manager.enqueue_change_2fa(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(require_token)])
async def retry_job(job_id: str) -> dict[str, Any]:
    try:
        assert_no_bulk_passkey_for_job(manager, job_id)
        return {"job": manager.retry(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/recheck", dependencies=[Depends(require_token)])
async def recheck_job(job_id: str) -> dict[str, Any]:
    try:
        assert_no_bulk_passkey_for_job(manager, job_id)
        return {"job": manager.recheck(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/refresh-usage", dependencies=[Depends(require_token)])
async def refresh_job_usage(job_id: str) -> dict[str, Any]:
    try:
        assert_no_bulk_passkey_for_job(manager, job_id)
        return {"job": await manager.refresh_usage(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TwoFAFlowError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/delete-chats", dependencies=[Depends(require_token)])
async def delete_job_chats(
    job_id: str,
    _request: DeleteChatsRequest,
) -> dict[str, Any]:
    try:
        assert_no_bulk_passkey_for_job(manager, job_id)
        return {"job": await manager.delete_all_chats(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TwoFAFlowError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/passkey/start", dependencies=[Depends(require_token)])
async def start_job_passkey(job_id: str, _request: PasskeyRequest) -> JSONResponse:
    try:
        job = manager.jobs[job_id]
        if job is not None and any(other.email.strip().casefold() == job.email.strip().casefold()
                                   and other.status in {"queued", "running"}
                                   for other in password_manager.jobs.values()):
            raise ValueError("Tài khoản đang đổi mật khẩu; vui lòng chờ")
        if passkey_manager.is_busy(job.email):
            raise ValueError("Tài khoản đang thêm passkey hàng loạt; vui lòng chờ")
        url = await manager.prepare_passkey(job_id)
        token = passkey_handoffs.issue(url)
        return JSONResponse(
            {"launch_path": f"/api/passkey/launch/{token}"},
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="Chưa thể thêm passkey; chờ thao tác đang chạy hoặc thử lại sau") from exc
    except TwoFAFlowError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/passkey/bootstrap", dependencies=[Depends(require_token)])
async def passkey_bootstrap() -> JSONResponse:
    return JSONResponse(
        {
            "jobs": passkey_manager.snapshots(),
            "worker_health": passkey_manager.worker_health(),
        },
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/passkey/jobs", dependencies=[Depends(require_token)])
async def add_passkey_jobs(request: PasskeyBatchRequest) -> dict[str, Any]:
    try:
        for line in request.lines:
            email = line.split("|", 1)[0]
            manager.assert_not_logging_out(email)
            if passkey_manager.is_busy(email):
                raise ValueError("Tài khoản đang thêm passkey hàng loạt; vui lòng chờ")
            if any(
                other.email.strip().casefold() == email.strip().casefold()
                and other.status in {"queued", "running"}
                for other in getattr(manager, "jobs", {}).values()
            ):
                raise ValueError("Tài khoản đang có thao tác 2FA; vui lòng chờ")
            if any(
                other.email.strip().casefold() == email.strip().casefold()
                and other.status in {"queued", "running"}
                for other in password_manager.jobs.values()
            ):
                raise ValueError("Tài khoản đang đổi mật khẩu; vui lòng chờ")
        return {"jobs": passkey_manager.add(request.lines)}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/passkey/jobs/{job_id}/launch", dependencies=[Depends(require_token)])
async def launch_passkey_job(job_id: str, _request: PasskeyLaunchRequest) -> JSONResponse:
    try:
        token = passkey_manager.issue_handoff(job_id, passkey_handoffs)
        launch_path = f"/api/passkey/launch/{token}"
        opened = False
        if _request.native_window:
            host = f"[{RUNTIME_HOST}]" if RUNTIME_HOST == "::1" else RUNTIME_HOST
            job = passkey_manager.jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            # Starting a separate Chrome process can take time; never block workers/SSE.
            opened = await asyncio.to_thread(
                passkey_windows.open,
                f"http://{host}:{RUNTIME_PORT}{launch_path}",
                index=job.window_index,
                total=job.window_total,
            )
        return JSONResponse(
            {"launch_path": launch_path, "opened": opened},
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy passkey job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/passkey/jobs/{job_id}/retry", dependencies=[Depends(require_token)])
async def retry_passkey_job(job_id: str) -> dict[str, Any]:
    try:
        return {"job": passkey_manager.retry(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy passkey job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/passkey/windows/close", dependencies=[Depends(require_token)])
async def close_passkey_windows() -> dict[str, int]:
    closed = await asyncio.to_thread(passkey_windows.close_all)
    return {"closed": closed}


@app.post("/api/passkey/jobs/{job_id}/stop", dependencies=[Depends(require_token)])
async def stop_passkey_job(job_id: str) -> dict[str, Any]:
    try:
        return {"job": passkey_manager.stop(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy passkey job") from exc


@app.delete("/api/passkey/jobs/{job_id}", dependencies=[Depends(require_token)])
async def delete_passkey_job(job_id: str) -> dict[str, bool]:
    try:
        passkey_manager.delete(job_id)
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy passkey job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/passkey/jobs", dependencies=[Depends(require_token)])
async def clear_passkey_jobs() -> dict[str, int]:
    try:
        return {"deleted": await passkey_manager.clear_async()}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/passkey/jobs/{job_id}/logs", dependencies=[Depends(require_token)])
async def passkey_job_logs(job_id: str) -> dict[str, Any]:
    try:
        return {"logs": passkey_manager.logs(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy passkey job") from exc


@app.get("/api/passkey/output", dependencies=[Depends(require_token)])
async def passkey_output() -> PlainTextResponse:
    body = "\n".join(passkey_manager.output())
    if body:
        body += "\n"
    return PlainTextResponse(
        body,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "attachment; filename=passkey-handoffs.txt",
        },
    )


@app.get("/api/passkey/events")
async def passkey_events(token: str):
    if not secrets.compare_digest(token, auth_token):
        raise HTTPException(status_code=401, detail="Token không hợp lệ")
    queue = passkey_manager.subscribe()

    async def stream():
        try:
            yield "data: " + json.dumps({
                "type": "snapshot",
                "jobs": passkey_manager.snapshots(),
                "worker_health": passkey_manager.worker_health(),
            }) + "\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            passkey_manager.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/passkey/launch/{launch_token}")
async def launch_passkey(launch_token: str):
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
               "X-Content-Type-Options": "nosniff"}
    try:
        return RedirectResponse(passkey_handoffs.consume(launch_token), status_code=303, headers=headers)
    except (KeyError, ValueError):
        return PlainTextResponse("Liên kết đã dùng hoặc hết hạn. Chuẩn bị passkey lại trong tool.", status_code=410, headers=headers)


@app.post("/api/jobs/{job_id}/logout-sessions", dependencies=[Depends(require_token)])
async def logout_job_sessions(job_id: str, _request: LogoutSessionsRequest) -> dict[str, Any]:
    try:
        job = getattr(manager, "jobs", {}).get(job_id)
        if job is not None:
            assert_no_bulk_passkey(job.email)
        if any(other.email.strip().casefold() == job.email.strip().casefold()
               and other.status in {"queued", "running"}
               for other in password_manager.jobs.values()):
            raise ValueError("Tài khoản đang đổi mật khẩu; vui lòng chờ trước khi logout")
        return {"job": await manager.logout_all_sessions(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TwoFAFlowError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/stop", dependencies=[Depends(require_token)])
async def stop_job(job_id: str) -> dict[str, Any]:
    try:
        return {"job": manager.stop(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job") from exc


@app.delete("/api/jobs/failed", dependencies=[Depends(require_token)])
async def clear_failed_jobs() -> dict[str, Any]:
    deleted = manager.clear_failed()
    return {"deleted": deleted, "jobs": manager.snapshots()}


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_token)])
async def delete_job(job_id: str) -> dict[str, bool]:
    try:
        manager.delete(job_id)
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/jobs/stop-all", dependencies=[Depends(require_token)])
async def stop_all() -> dict[str, bool]:
    manager.stop_all()
    return {"ok": True}


@app.delete("/api/jobs", dependencies=[Depends(require_token)])
async def clear_jobs() -> dict[str, int]:
    try:
        return {"deleted": await manager.clear_async()}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}/logs", dependencies=[Depends(require_token)])
async def job_logs(job_id: str) -> dict[str, Any]:
    try:
        return {"logs": manager.logs(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job") from exc


@app.get("/api/output", dependencies=[Depends(require_token)])
async def output_file() -> PlainTextResponse:
    body = "\n".join(manager.output())
    if body:
        body += "\n"
    return PlainTextResponse(
        body,
        headers={"Content-Disposition": "attachment; filename=twofa-success.txt"},
    )


@app.put("/api/settings", dependencies=[Depends(require_token)])
async def update_settings(request: SettingsRequest) -> dict[str, Any]:
    try:
        settings = await manager.update_settings({
            "twofa.max_concurrent": request.max_concurrent,
            "twofa.job_timeout": request.job_timeout,
            "twofa.auto_retry": request.auto_retry,
            "twofa.auto_retry_max": request.auto_retry_max,
            "twofa.auto_retry_delay": request.auto_retry_delay,
            "twofa.change_enabled": request.change_enabled,
            "twofa.read_usage": request.read_usage,
            "twofa.read_payment_methods": request.read_payment_methods,
            "twofa.input_draft": request.input_draft,
        })
        passkey_manager.configure(
            max_concurrent=int(settings["twofa.max_concurrent"]),
            job_timeout=float(settings["twofa.job_timeout"]),
        )
        return {"settings": settings}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/password/bootstrap", dependencies=[Depends(require_token)])
async def password_bootstrap() -> JSONResponse:
    target_password = settings_repo.get("password_change.target_password")
    configured = isinstance(target_password, str)
    return JSONResponse(
        {
            "configured": configured,
            "target_password": target_password if configured else "",
            "jobs": password_manager.snapshots(),
            "worker_health": password_manager.worker_health(),
        },
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/password-settings", dependencies=[Depends(require_token)])
async def password_settings_status(reveal: bool = False) -> JSONResponse:
    target_password = settings_repo.get("password_change.target_password")
    configured = isinstance(target_password, str)
    payload: dict[str, Any] = {"configured": configured}
    if reveal:
        payload["target_password"] = target_password if configured else ""
    return JSONResponse(
        payload,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.put("/api/password-settings", dependencies=[Depends(require_token)])
async def update_password_settings(request: PasswordSettingsRequest) -> JSONResponse:
    target_password = request.target_password.get_secret_value()
    if (
        not 12 <= len(target_password) <= 128
        or "|" in target_password
        or any(ord(char) < 32 or ord(char) == 127 for char in target_password)
    ):
        raise HTTPException(
            status_code=422,
            detail="Mật khẩu chung phải dài 12-128 ký tự và không chứa ký tự cấm",
        )
    try:
        settings_repo.set(
            "password_change.target_password",
            target_password,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Không lưu được cấu hình mật khẩu vào cơ sở dữ liệu",
        ) from exc
    return JSONResponse(
        {"configured": True},
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.delete("/api/password-settings", dependencies=[Depends(require_token)])
async def clear_password_settings() -> JSONResponse:
    settings_repo.delete("password_change.target_password")
    return JSONResponse(
        {"configured": False},
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/password/jobs", dependencies=[Depends(require_token)])
async def add_password_jobs(request: PasswordBatchRequest) -> dict[str, Any]:
    try:
        for line in request.lines:
            email = line.split("|", 1)[0]
            manager.assert_not_logging_out(email)
            assert_no_bulk_passkey(email)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        return {"jobs": password_manager.add(request.lines)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/password/jobs/{job_id}/retry", dependencies=[Depends(require_token)])
async def retry_password_job(job_id: str) -> dict[str, Any]:
    try:
        job = password_manager.jobs.get(job_id)
        if job is not None:
            manager.assert_not_logging_out(job.email)
            assert_no_bulk_passkey(job.email)
        return {"job": password_manager.retry(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy password job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/password/jobs/{job_id}/stop", dependencies=[Depends(require_token)])
async def stop_password_job(job_id: str) -> dict[str, Any]:
    try:
        return {"job": password_manager.stop(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy password job") from exc


@app.delete("/api/password/jobs/{job_id}", dependencies=[Depends(require_token)])
async def delete_password_job(job_id: str) -> dict[str, bool]:
    try:
        password_manager.delete(job_id)
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy password job") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/password/jobs", dependencies=[Depends(require_token)])
async def clear_password_jobs() -> dict[str, int]:
    try:
        return {"deleted": await password_manager.clear_async()}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/password/jobs/{job_id}/logs", dependencies=[Depends(require_token)])
async def password_job_logs(job_id: str) -> dict[str, Any]:
    try:
        return {"logs": password_manager.logs(job_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy password job") from exc


@app.get("/api/password/output", dependencies=[Depends(require_token)])
async def password_output() -> PlainTextResponse:
    body = "\n".join(password_manager.output())
    if body:
        body += "\n"
    return PlainTextResponse(
        body,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "attachment; filename=password-success.txt",
        },
    )


@app.get("/api/password/history", dependencies=[Depends(require_token)])
async def password_history() -> JSONResponse:
    history = password_manager.history()
    return JSONResponse(
        {"count": len(history), "history": history},
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/password/events")
async def password_events(token: str):
    if not secrets.compare_digest(token, auth_token):
        raise HTTPException(status_code=401, detail="Token không hợp lệ")
    queue = password_manager.subscribe()

    async def stream():
        try:
            yield "data: " + json.dumps({
                "type": "snapshot",
                "jobs": password_manager.snapshots(),
                "worker_health": password_manager.worker_health(),
            }) + "\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            password_manager.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/api/events")
async def events(token: str):
    if not secrets.compare_digest(token, auth_token):
        raise HTTPException(status_code=401, detail="Token không hợp lệ")
    queue = manager.subscribe()

    async def stream():
        try:
            yield f"data: {json.dumps({'type': 'snapshot', 'jobs': manager.snapshots(), 'worker_health': manager.worker_health()})}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            manager.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")


app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/passkey")
def passkey_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "passkey.html")


def _open_browser_when_ready(host: str, port: int) -> None:
    url = f"http://{host if host != '::1' else '127.0.0.1'}:{port}/"

    def worker() -> None:
        health = f"{url}api/health"
        for _ in range(50):
            try:
                from urllib.request import urlopen

                with urlopen(health, timeout=0.5) as response:
                    if response.status == 200:
                        webbrowser.open(url)
                        return
            except Exception:
                time.sleep(0.2)

    threading.Thread(target=worker, name="twofa-browser", daemon=True).start()


def main() -> None:
    global RUNTIME_PORT, RUNTIME_HOST
    parser = argparse.ArgumentParser(description="Shoptaikhoan Tool localhost")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5033)
    parser.add_argument("--uds")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check-runtime-dependencies", action="store_true")
    args = parser.parse_args()
    if args.check_runtime_dependencies:
        from cryptography.hazmat.primitives import serialization
        import request_phase
        import sentinel_pow
        import sentinel_quickjs

        script = sentinel_quickjs._quickjs_script_path()
        required = (
            request_phase._get_sentinel_token,
            sentinel_pow.get_sentinel_token,
            sentinel_quickjs.get_sentinel_token_via_quickjs,
            serialization.load_pem_public_key,
        )
        if not all(callable(item) for item in required):
            raise RuntimeError("Pure-request login dependency contract is incomplete")
        if not script.is_file():
            raise FileNotFoundError(f"Sentinel runtime asset missing: {script}")
        if sys.platform == "darwin" and getattr(sys, "frozen", False):
            if not menu_bar_command(args.host, args.port):
                raise FileNotFoundError("Native macOS menu-bar helper is missing")
        print("PASS: pure-request login dependencies and Sentinel runtime asset")
        return
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("Server chỉ được bind localhost")
    if not 1 <= args.port <= 65535:
        parser.error("Port phải nằm trong khoảng 1..65535")
    RUNTIME_PORT = args.port
    RUNTIME_HOST = "twofa.localhost" if args.uds else args.host
    import uvicorn

    menu_bar_process = None if args.uds or os.environ.get("SHOPTAIKHOAN_NO_MENU_BAR") == "1" else launch_menu_bar(args.host, args.port)
    if not args.no_browser and not args.uds:
        _open_browser_when_ready(args.host, args.port)
    # A browser keeps the SSE endpoint open indefinitely. Bound graceful
    # shutdown so Quit can cancel that connection and fully terminate the
    # Finder-launched app instead of leaving a zombie Dock process.
    try:
        if args.uds:
            socket_path = Path(args.uds)
            socket_path.parent.mkdir(parents=True, exist_ok=True)
            socket_path.unlink(missing_ok=True)
            uvicorn.run(app, uds=str(socket_path), log_level="warning", timeout_graceful_shutdown=3)
        else:
            uvicorn.run(app, host=args.host, port=args.port, log_level="warning", timeout_graceful_shutdown=3)
    finally:
        if args.uds:
            Path(args.uds).unlink(missing_ok=True)
        if menu_bar_process is not None and menu_bar_process.poll() is None:
            menu_bar_process.terminate()


if __name__ == "__main__":
    main()
