"""Localhost GUI server (127.0.0.1 only)."""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import socketserver
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, request
from urllib.parse import urlencode, urlparse

from gpt_tool.convert import FORMATS
from gpt_tool.export import convert_bulk, export_bulk, outcome_dict
from gpt_tool.redaction import redact

def _module_root(module_file: Path, *, compiled: bool) -> Path:
    return module_file.parent if compiled else module_file.parent.parent


SOURCE_ROOT = _module_root(Path(__file__).resolve(), compiled="__compiled__" in globals())


def _runtime_paths(*, frozen: bool, bundle_root: Path, home: Path) -> tuple[Path, Path]:
    web = bundle_root / "web"
    configured = os.environ.get("GPT_TOOL_DATA_DIR")
    if configured:
        return web, Path(configured).expanduser() / "out"
    if frozen:
        data_root = Path(os.environ.get("GPT_TOOL_DATA_DIR") or home / "GPT-Tool")
        return web, data_root / "out"
    return web, bundle_root / "out"


FROZEN = bool(getattr(sys, "frozen", False) or "__compiled__" in globals())
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))
WEB, OUT = _runtime_paths(frozen=FROZEN, bundle_root=BUNDLE_ROOT, home=Path.home())
HOST = "127.0.0.1"
PORT = int(os.environ.get("GPT_TOOL_PORT", "8765"))
SOCKET_PATH = os.environ.get("GPT_TOOL_SOCKET", "")
PUBLIC_URL = os.environ.get("GPT_TOOL_PUBLIC_URL", "")

_jobs: dict[str, dict] = {}
_job_cancellations: dict[str, threading.Event] = {}
_cockpit_imports: dict[str, list[object]] = {}
_lock = threading.Lock()
MAX_ARTIFACT_BYTES = 5 * 1024 * 1024


class ArtifactError(ValueError):
    pass


def _artifact_payload(result: dict) -> object:
    raw_path = result.get("path")
    if not result.get("ok") or not raw_path:
        raise ArtifactError("JSON không khả dụng")
    try:
        target = Path(str(raw_path)).resolve(strict=True)
        out_root = OUT.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ArtifactError("JSON không khả dụng") from exc
    if not target.is_relative_to(out_root) or target.suffix.lower() != ".json" or not target.is_file():
        raise ArtifactError("JSON không khả dụng")
    try:
        if target.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ArtifactError("JSON quá lớn để sao chép")
        return json.loads(target.read_text(encoding="utf-8"))
    except ArtifactError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactError("JSON không khả dụng") from exc


def _copy_payload(job: dict, target: str) -> object:
    results = list(job.get("results") or [])
    successful = [item for item in results if item.get("ok") and item.get("path")]
    if target == "all":
        if not successful:
            raise ArtifactError("Chưa có JSON để sao chép")
        successful.sort(key=lambda item: item.get("index") if item.get("index") is not None else 10**9)
        return [_artifact_payload(item) for item in successful]
    if not target.isdigit():
        raise ArtifactError("JSON không khả dụng")
    index = int(target)
    result = next((item for item in successful if item.get("index") == index), None)
    if result is None:
        raise ArtifactError("JSON không khả dụng")
    return _artifact_payload(result)


def _public_job(job: dict) -> dict:
    public = {key: value for key, value in job.items() if key != "results"}
    public["results"] = []
    for result in job.get("results") or []:
        item = {
            key: value
            for key, value in result.items()
            if key not in {"path", "cockpit_payload", "nine_router_payload"}
        }
        item["copyable"] = bool(result.get("ok") and result.get("path"))
        public["results"].append(item)
    return public


def _take_cockpit_import(token: str) -> list[object] | None:
    with _lock:
        return _cockpit_imports.pop(token, None)


def _open_url(url: str) -> None:
    if sys.platform == "win32":
        os.startfile(url)  # type: ignore[attr-defined]
        return
    command = "open" if sys.platform == "darwin" else "xdg-open"
    result = subprocess.run([command, url], check=False)
    if result.returncode:
        raise OSError("Không thể mở Cockpit Tools")


def _queue_cockpit_import(bundle: list[object], base_url: str) -> int:
    token = uuid.uuid4().hex
    with _lock:
        _cockpit_imports[token] = bundle
    import_url = f"{base_url}/api/cockpit/{token}"
    deep_link = "cockpit-tools://provider-import?" + urlencode(
        {"platform": "codex", "import_url": import_url, "auto_import": "true"}
    )
    try:
        _open_url(deep_link)
    except Exception:
        _take_cockpit_import(token)
        raise
    return len(bundle)


def _send_job_to_cockpit(job_id: str, base_url: str) -> None:
    with _lock:
        job = _jobs[job_id]
        results = list(job.get("results") or [])
    results.sort(key=lambda item: item.get("index") if item.get("index") is not None else 10**9)
    bundle = [item["cockpit_payload"] for item in results if item.get("ok") and item.get("cockpit_payload")]
    if not bundle:
        return
    try:
        count = _queue_cockpit_import(bundle, base_url)
    except Exception as exc:
        with _lock:
            job["cockpit"].update(error=redact(str(exc)), sent=False)
        return
    with _lock:
        job["cockpit"].update(count=count, sent=True)
        for item in job["results"]:
            if item.get("ok") and item.get("cockpit_payload"):
                item["cockpit_sent"] = True


def _nine_router_cli_token(data_dir: Path | None = None) -> str:
    if data_dir is None:
        configured = os.environ.get("NINE_ROUTER_DATA_DIR")
        if configured:
            data_dir = Path(configured).expanduser()
        elif sys.platform == "win32":
            data_dir = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "9router"
        else:
            data_dir = Path.home() / ".9router"
    try:
        machine_id = (data_dir / "machine-id").read_text(encoding="utf-8").strip()
        secret = (data_dir / "auth" / "cli-secret").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("Không tìm thấy xác thực cục bộ của 9Router; hãy mở 9Router trước") from exc
    if not machine_id or not secret:
        raise RuntimeError("Xác thực cục bộ của 9Router không hợp lệ")
    return hashlib.sha256(f"{machine_id}9r-cli-auth{secret}".encode()).hexdigest()[:16]


def _import_nine_router(bundle: list[object]) -> dict:
    endpoint = "http://127.0.0.1:20128/api/oauth/codex/bulk-import"
    body = json.dumps({"accounts": bundle}, ensure_ascii=False).encode()
    req = request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "x-9r-cli-token": _nine_router_cli_token()},
    )
    try:
        with request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError("9Router cần phiên bản 0.4.80 trở lên") from exc
        raise RuntimeError(f"9Router từ chối yêu cầu (HTTP {exc.code})") from exc
    except (error.URLError, TimeoutError) as exc:
        raise RuntimeError("Không kết nối được 9Router tại 127.0.0.1:20128") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("9Router trả về dữ liệu không hợp lệ") from exc
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        raise RuntimeError("9Router trả về dữ liệu không hợp lệ")
    return result


def _send_job_to_nine_router(job_id: str) -> None:
    with _lock:
        job = _jobs[job_id]
        targets = [
            item
            for item in job.get("results") or []
            if item.get("ok") and item.get("nine_router_payload")
        ]
    targets.sort(key=lambda item: item.get("index") if item.get("index") is not None else 10**9)
    if not targets:
        return
    try:
        response = _import_nine_router([item["nine_router_payload"] for item in targets])
    except Exception as exc:
        message = redact(str(exc))
        with _lock:
            job["nine_router"].update(error=message, sent=False)
            for item in targets:
                item["nine_router_error"] = message
        return

    api_results = {item.get("index"): item for item in response["results"] if isinstance(item, dict)}
    with _lock:
        for index, item in enumerate(targets):
            imported = api_results.get(index, {})
            if imported.get("ok"):
                item["nine_router_sent"] = True
            else:
                item["nine_router_error"] = redact(str(imported.get("error") or "Không thể thêm vào 9Router"))
        success = sum(1 for item in targets if item.get("nine_router_sent"))
        failed = len(targets) - success
        job["nine_router"].update(count=success, failed=failed, sent=success > 0)
        if failed:
            job["nine_router"]["error"] = f"9Router: {failed} tài khoản thêm thất bại"


def _completed_job(outcomes: list) -> dict:
    results = []
    for index, outcome in enumerate(outcomes):
        item = outcome_dict(outcome)
        if item.get("index") is None:
            item["index"] = index
        results.append(item)
    return {
        "done": True,
        "results": results,
        "running": {},
        "total": len(results),
        "workers": 0,
        "error": None,
    }


def _json(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(n) if n else b"{}"
    return json.loads(raw.decode("utf-8") or "{}")


def _origin_allowed(handler: BaseHTTPRequestHandler) -> bool:
    origin = handler.headers.get("Origin")
    if PUBLIC_URL:
        return not origin or origin == PUBLIC_URL
    port = handler.server.server_address[1]
    return not origin or origin in {f"http://{HOST}:{port}", f"http://localhost:{port}"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        if sys.stderr is not None:
            message = re.sub(r"/api/cockpit/[A-Za-z0-9_-]+", "/api/cockpit/[redacted]", fmt % args)
            sys.stderr.write("gui: " + message + "\n")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/api/cockpit/"):
            bundle = _take_cockpit_import(path.rsplit("/", 1)[-1])
            if bundle is None:
                _json(self, 404, {"error": "import bundle not found"})
                return
            _json(self, 200, bundle)
            return
        if path == "/api/health":
            _json(self, 200, {"ok": True, "formats": list(FORMATS)})
            return
        if path.startswith("/api/jobs/"):
            parts = path.strip("/").split("/")
            job_id = parts[2] if len(parts) >= 3 else ""
            with _lock:
                job = _jobs.get(job_id)
            if not job:
                _json(self, 404, {"error": "job not found"})
                return
            if len(parts) == 3:
                _json(self, 200, _public_job(job))
                return
            if len(parts) == 5 and parts[3] == "copy":
                try:
                    data = _copy_payload(job, parts[4])
                except ArtifactError as exc:
                    _json(self, 404, {"error": str(exc)})
                    return
                _json(self, 200, {"ok": True, "data": data})
                return
            _json(self, 404, {"error": "not found"})
            return
        rel = "index.html" if path in {"/", "/index.html"} else path.lstrip("/")
        target = (WEB / rel).resolve()
        if not str(target).startswith(str(WEB.resolve())) or not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        ctype = "text/html; charset=utf-8"
        if target.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif target.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/shutdown":
            if not _origin_allowed(self):
                _json(self, 403, {"error": "forbidden"})
                return
            _json(self, 200, {"ok": True})
            self.server.shutdown()
            return
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] == "stop":
            if not _origin_allowed(self):
                _json(self, 403, {"error": "forbidden"})
                return
            with _lock:
                job = _jobs.get(parts[2])
                cancel_event = _job_cancellations.get(parts[2])
                if job and cancel_event:
                    job["cancelling"] = True
                    cancel_event.set()
            if not job or not cancel_event:
                _json(self, 404, {"error": "job not found"})
                return
            _json(self, 200, {"ok": True})
            return
        try:
            body = _read_json(self)
        except json.JSONDecodeError:
            _json(self, 400, {"error": "invalid JSON"})
            return
        if path == "/api/open-out":
            OUT.mkdir(parents=True, exist_ok=True)
            _open_folder(OUT)
            _json(self, 200, {"ok": True, "path": str(OUT)})
            return
        if path == "/api/convert":
            fmt = (body.get("format") or "").strip()
            text = body.get("text") or ""
            if fmt not in FORMATS:
                _json(self, 400, {"error": "chọn format trước khi chạy"})
                return
            try:
                job = _completed_job(convert_bulk(text, fmt, OUT))
            except Exception as exc:
                _json(self, 400, {"error": str(exc)})
                return
            job_id = uuid.uuid4().hex[:12]
            job["id"] = job_id
            with _lock:
                _jobs[job_id] = job
            public = _public_job(job)
            _json(self, 200, {"ok": True, "id": job_id, "results": public["results"]})
            return
        if path == "/api/export":
            fmt = (body.get("format") or "").strip()
            if fmt not in FORMATS:
                _json(self, 400, {"error": "chọn format trước khi chạy"})
                return
            lines = (body.get("lines") or "").splitlines()
            proxy = (body.get("proxy") or "").strip() or None
            workers = max(1, min(8, int(body.get("workers") or 2)))
            cockpit_import = body.get("cockpit_import") is True
            nine_router_import = body.get("nine_router_import") is True
            job_id = uuid.uuid4().hex[:12]
            jobs = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
            base_url = PUBLIC_URL or f"http://{HOST}:{self.server.server_address[1]}"
            cancel_event = threading.Event()
            with _lock:
                _jobs[job_id] = {
                    "id": job_id,
                    "done": False,
                    "results": [],
                    "running": {},
                    "total": len(jobs),
                    "workers": workers,
                    "error": None,
                    "cancelling": False,
                    "cancelled": False,
                    "cockpit": {"enabled": cockpit_import, "sent": False, "count": 0, "error": None},
                    "nine_router": {
                        "enabled": nine_router_import,
                        "sent": False,
                        "count": 0,
                        "failed": 0,
                        "error": None,
                    },
                }
                _job_cancellations[job_id] = cancel_event

            def run() -> None:
                def on_step(email: str, step: str, index=None) -> None:
                    with _lock:
                        key = str(index) if index is not None else email
                        _jobs[job_id]["running"][key] = {"email": email, "step": step, "index": index}

                def on_progress(outcome) -> None:
                    with _lock:
                        running = _jobs[job_id]["running"]
                        if outcome.index is not None:
                            running.pop(str(outcome.index), None)
                        running.pop(outcome.email, None)
                        _jobs[job_id]["results"].append(outcome_dict(outcome))

                try:
                    export_bulk(
                        lines,
                        fmt,
                        OUT,
                        proxy,
                        workers,
                        on_progress,
                        on_step,
                        cockpit_import=cockpit_import,
                        nine_router_import=nine_router_import,
                        cancel_event=cancel_event,
                    )
                    if cockpit_import and not cancel_event.is_set():
                        _send_job_to_cockpit(job_id, base_url)
                    if nine_router_import and not cancel_event.is_set():
                        _send_job_to_nine_router(job_id)
                except Exception as exc:
                    with _lock:
                        _jobs[job_id]["error"] = str(exc)
                finally:
                    with _lock:
                        _jobs[job_id]["done"] = True
                        _jobs[job_id]["cancelled"] = cancel_event.is_set()
                        _jobs[job_id]["running"] = {}
                        _job_cancellations.pop(job_id, None)

            threading.Thread(target=run, daemon=True).start()
            _json(self, 200, {"id": job_id})
            return
        self.send_error(404)


def _open_folder(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    elif sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def _bind(host: str, start_port: int, attempts: int = 20) -> tuple[ThreadingHTTPServer, int]:
    last: OSError | None = None
    for port in range(start_port, start_port + max(1, attempts)):
        try:
            return ThreadingHTTPServer((host, port), Handler), port
        except OSError as exc:
            last = exc
            if getattr(exc, "errno", None) not in {48, 98, 10048}:
                raise
    raise OSError(f"hết cổng trống từ {start_port}–{start_port + attempts - 1}") from last


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main() -> None:
    if not FROZEN:
        from gpt_tool.ensure_deps import ensure_deps

        ensure_deps()
    WEB.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH:
        socket_path = Path(SOCKET_PATH)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        httpd = ThreadingUnixHTTPServer(str(socket_path), Handler)
        httpd.server_name, httpd.server_port = "localhost", 0
        url = PUBLIC_URL + "/"
    else:
        httpd, port = _bind(HOST, PORT)
        url = f"http://{HOST}:{port}/"
        if port != PORT:
            print(f"Cổng {PORT} bận → dùng {port}.", flush=True)
    print(f"GPT-Tool GUI → {url}", flush=True)
    if os.environ.get("GPT_TOOL_NO_OPEN_BROWSER") != "1":
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
        if SOCKET_PATH:
            Path(SOCKET_PATH).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
