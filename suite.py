"""One launcher and dashboard for the three isolated Shoptaikhoan modules."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
HUB_PORT = 5050


@dataclass(frozen=True)
class Module:
    key: str
    label: str
    public_host: str
    socket_path: Path
    cwd: Path
    command: tuple[str, ...]
    env: dict[str, str]


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float | None = None):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
            self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.socket_path))


def build_modules(root: Path = ROOT, python: Path | None = None) -> tuple[Module, ...]:
    executable = str(python or Path(sys.executable))
    runtime = Path(os.environ.get("SHOPTAIKHOAN_SUITE_DATA_DIR") or root / "runtime").expanduser()
    sockets = runtime / "s"
    twofa_executable = os.environ.get("SHOPTAIKHOAN_SUITE_TWOFA_EXECUTABLE")
    export_executable = os.environ.get("SHOPTAIKHOAN_SUITE_EXPORT_EXECUTABLE")
    browser_executable = os.environ.get("SHOPTAIKHOAN_SUITE_BROWSER_EXECUTABLE")
    return (
        Module(
            "twofa",
            "2FA & Password",
            "twofa.localhost",
            sockets / "twofa.sock",
            Path(twofa_executable).parent if twofa_executable else root / "modules" / "twofa",
            (twofa_executable, "--uds", str(sockets / "twofa.sock"), "--port", str(HUB_PORT), "--no-browser") if twofa_executable else (executable, "change 2fa community/server.py", "--uds", str(sockets / "twofa.sock"), "--port", str(HUB_PORT), "--no-browser"),
            {"SHOPTAIKHOAN_TWOFA_DATA_DIR": str(runtime / "twofa")},
        ),
        Module(
            "export",
            "Codex Export",
            "export.localhost",
            sockets / "export.sock",
            Path(export_executable).parent if export_executable else root / "modules" / "gpt-tool",
            (export_executable,) if export_executable else (executable, "-m", "gpt_tool.server"),
            {
                "GPT_TOOL_SOCKET": str(sockets / "export.sock"),
                "GPT_TOOL_PUBLIC_URL": f"http://export.localhost:{HUB_PORT}",
                "GPT_TOOL_DATA_DIR": str(runtime / "gpt-tool"),
            },
        ),
        Module(
            "browser",
            "Browser Login & 9Router",
            "browser.localhost",
            sockets / "browser.sock",
            Path(browser_executable).parent if browser_executable else root / "modules" / "browser-login",
            (browser_executable,) if browser_executable else (executable, "server.py"),
            {
                "SHOPTAIKHOAN_SOCKET": str(sockets / "browser.sock"),
                "SHOPTAIKHOAN_PUBLIC_URL": f"http://browser.localhost:{HUB_PORT}",
                "SHOPTAIKHOAN_DATA_DIR": str(runtime / "browser-login"),
                "SHOPTAIKHOAN_IMPORT_API": f"http://browser.localhost:{HUB_PORT}/api/import",
            },
        ),
    )


def probe(socket_path: Path) -> tuple[bool, str]:
    connection = UnixHTTPConnection(socket_path, timeout=0.6)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        response.read()
        return response.status == 200, ""
    except Exception as exc:
        return False, type(exc).__name__
    finally:
        connection.close()


class SuiteManager:
    def __init__(self, modules: Iterable[Module], probe: Callable[[Path], tuple[bool, str]] = probe):
        self.modules = tuple(modules)
        self.probe = probe
        self.processes: dict[str, subprocess.Popen] = {}

    def start(self) -> None:
        common_env = {
            "PYTHONUNBUFFERED": "1",
            "SHOPTAIKHOAN_NO_OPEN_BROWSER": "1",
            "GPT_TOOL_NO_OPEN_BROWSER": "1",
        }
        for module in self.modules:
            module.socket_path.parent.mkdir(parents=True, exist_ok=True)
            module.socket_path.unlink(missing_ok=True)
            for key, value in module.env.items():
                if key.endswith("DATA_DIR"):
                    Path(value).mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update(common_env)
            env.update(module.env)
            self.processes[module.key] = subprocess.Popen(module.command, cwd=module.cwd, env=env)

    def status(self) -> dict[str, list[dict[str, object]]]:
        modules = []
        for module in self.modules:
            ready, _detail = self.probe(module.socket_path)
            modules.append({"key": module.key, "label": module.label, "ready": ready})
        return {"modules": modules}

    def stop(self) -> None:
        for process in self.processes.values():
            if process.poll() is None:
                process.terminate()
        for process in self.processes.values():
            if process.poll() is not None:
                continue
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        self.processes.clear()
        for module in self.modules:
            module.socket_path.unlink(missing_ok=True)


def create_hub_server(host: str, port: int, manager: SuiteManager, web_root: Path) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _module(self) -> Module | None:
            request_host = self.headers.get("Host", "").split(":", 1)[0].lower()
            return next((module for module in manager.modules if module.public_host == request_host), None)

        def _proxy(self, module: Module) -> None:
            hop_headers = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {key: value for key, value in self.headers.items() if key.lower() not in hop_headers and key.lower() != "host"}
            headers["Host"] = module.public_host
            headers["Connection"] = "close"
            connection = UnixHTTPConnection(module.socket_path)
            try:
                connection.request(self.command, self.path, body=body, headers=headers)
                response = connection.getresponse()
                self.send_response(response.status, response.reason)
                for key, value in response.getheaders():
                    if key.lower() not in hop_headers:
                        self.send_header(key, value)
                self.send_header("Connection", "close")
                self.end_headers()
                while chunk := response.read1(64 * 1024):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except OSError:
                self._send(502, b'Module unavailable', "text/plain; charset=utf-8")
            finally:
                self.close_connection = True
                connection.close()

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if module := self._module():
                self._proxy(module)
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/status":
                body = json.dumps(manager.status(), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
                return
            files = {
                "/": (web_root / "index.html", "text/html; charset=utf-8"),
                "/app.js": (web_root / "app.js", "text/javascript; charset=utf-8"),
                "/styles.css": (web_root / "styles.css", "text/css; charset=utf-8"),
                "/favicon.svg": (web_root / "favicon.svg", "image/svg+xml"),
            }
            target = files.get(path)
            if target and target[0].is_file():
                self._send(200, target[0].read_bytes(), target[1])
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            if module := self._module():
                self._proxy(module)
                return
            if self.path.split("?", 1)[0] != "/api/shutdown":
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            self._send(200, b'{"ok": true}', "application/json; charset=utf-8")
            threading.Thread(target=self.server.shutdown, name="suite-shutdown", daemon=True).start()

        def do_PUT(self) -> None:
            if module := self._module():
                self._proxy(module)
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def do_DELETE(self) -> None:
            if module := self._module():
                self._proxy(module)
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def do_OPTIONS(self) -> None:
            if module := self._module():
                self._proxy(module)
                return
            self._send(404, b"Not found", "text/plain; charset=utf-8")

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)


def start_server_thread(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name="suite-hub", daemon=True)
    thread.start()
    return thread


def main() -> None:
    parser = argparse.ArgumentParser(description="Shoptaikhoan Suite")
    parser.add_argument("--port", type=int, default=HUB_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    manager = SuiteManager(build_modules())
    server = create_hub_server(HOST, args.port, manager, ROOT / "web")
    manager.start()
    def terminate(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"Shoptaikhoan Suite → {url}", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.stop()


if __name__ == "__main__":
    main()
