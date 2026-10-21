from __future__ import annotations

import argparse
import json
import os
import socketserver
import threading
import uuid
from copy import deepcopy
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from .automation import BackupStopped, RunOptions, run_backup, safe_error
from .credentials import CredentialError, mask_email, parse_credentials
from .storage import BackupStore


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
DATA_DIR = Path(os.environ.get("CHAT_BACKUP_DATA_DIR") or ROOT / "runtime").expanduser()
SOCKET_PATH = Path(os.environ.get("CHAT_BACKUP_SOCKET") or DATA_DIR / "chat-backup.sock").expanduser()
MAX_REQUEST_BYTES = 32 * 1024


class JobController:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._selection_ready = threading.Event()
        self._conversations: list[dict[str, str]] = []
        self._selected_ids: set[str] = set()
        self._state: dict[str, object] = self._idle_state()

    @staticmethod
    def _idle_state() -> dict[str, object]:
        return {
            "running": False,
            "run_id": "",
            "stage": "idle",
            "message": "Sẵn sàng",
            "source": "",
            "target": "",
            "total": 0,
            "current": 0,
            "done": 0,
            "failed": 0,
            "skipped": 0,
            "cleanup_failed": 0,
        }

    def status(self) -> dict[str, object]:
        with self._lock:
            return deepcopy(self._state)

    def conversations(self) -> dict[str, object]:
        with self._lock:
            return {
                "run_id": self._state["run_id"],
                "items": deepcopy(self._conversations),
            }

    def _choose_conversations(self, conversations: list[dict[str, str]]) -> list[dict[str, str]]:
        with self._lock:
            if self._stop_event.is_set():
                raise BackupStopped("Đã dừng theo yêu cầu")
            self._conversations = deepcopy(conversations)
            self._state.update({
                "stage": "choosing",
                "message": "Tích chọn cuộc hội thoại rồi bấm Backup các chat đã chọn",
                "total": len(conversations),
            })
        while not self._selection_ready.wait(0.25):
            if self._stop_event.is_set():
                raise BackupStopped("Đã dừng theo yêu cầu")
        if self._stop_event.is_set():
            raise BackupStopped("Đã dừng theo yêu cầu")
        with self._lock:
            return [item for item in conversations if item["id"] in self._selected_ids]

    def select(self, payload: dict[str, object]) -> dict[str, object]:
        with self._lock:
            if self._state["stage"] != "choosing" or self._stop_event.is_set():
                raise RuntimeError("Không có danh sách hội thoại đang chờ chọn")
            if payload.get("run_id") != self._state["run_id"]:
                raise RuntimeError("Danh sách đã thay đổi, hãy tải lại trang")
            selected = payload.get("conversation_ids")
            if not isinstance(selected, list) or not selected or len(selected) > 10_000:
                raise ValueError("Hãy chọn từ 1 đến 10000 cuộc hội thoại")
            known_ids = {item["id"] for item in self._conversations}
            if any(not isinstance(item, str) or item not in known_ids for item in selected):
                raise ValueError("Cuộc hội thoại được chọn không nằm trong danh sách nguồn")
            self._selected_ids = set(selected)
            self._state.update({"stage": "selected", "message": "Đang chuẩn bị backup các chat đã chọn"})
            self._selection_ready.set()
            return self.status()

    def _event(self, stage: str, message: str) -> None:
        with self._lock:
            self._state["stage"] = stage
            if stage == "total":
                self._state["total"] = int(message or 0)
                self._state["message"] = "Đã đọc danh sách cuộc trò chuyện"
            elif stage == "progress":
                current, _, total = message.partition("/")
                self._state["current"] = int(current or 0)
                self._state["total"] = int(total or self._state["total"] or 0)
                self._state["message"] = "Đang sao chép cuộc trò chuyện"
            elif stage == "item_error":
                self._state["message"] = "Một cuộc trò chuyện chưa sao chép được; đang tiếp tục"
            elif stage in ("item_done", "item_failed", "item_skipped", "cleanup_failed"):
                key = {
                    "item_done": "done",
                    "item_failed": "failed",
                    "item_skipped": "skipped",
                    "cleanup_failed": "cleanup_failed",
                }[stage]
                self._state[key] = int(message or 0)
                if stage == "item_done":
                    self._state["message"] = f"Đã lưu {message} cuộc trò chuyện"
                elif stage == "item_skipped":
                    self._state["message"] = "Đã có checkpoint; bỏ qua, không copy trùng"
            else:
                self._state["message"] = message

    def start(self, payload: dict[str, object], *, choose: bool = False) -> dict[str, object]:
        source = parse_credentials(str(payload.get("source") or ""))
        target = parse_credentials(str(payload.get("target") or ""))
        if source.email == target.email:
            raise CredentialError("Tài khoản nguồn và đích phải khác nhau")
        marker = str(payload.get("marker_message") or "Đã lưu bản sao từ tài khoản nguồn.").strip()
        if not marker or len(marker) > 500:
            raise ValueError("Tin nhắn lưu bản sao phải dài từ 1 đến 500 ký tự")
        try:
            limit = int(payload.get("limit") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Giới hạn số chat không hợp lệ") from exc
        if limit < 0 or limit > 10_000:
            raise ValueError("Giới hạn số chat phải từ 0 đến 10000")
        options = RunOptions(
            marker_message=marker,
            cleanup_new_links=payload.get("cleanup_links") is not False,
            limit=limit,
            headless=payload.get("headless") is True,
        )

        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("Một tiến trình backup đang chạy")
            self._stop_event = threading.Event()
            self._selection_ready = threading.Event()
            self._conversations = []
            self._selected_ids = set()
            self._state = self._idle_state()
            self._state.update({
                "running": True,
                "run_id": uuid.uuid4().hex,
                "stage": "starting",
                "message": "Đang khởi động browser riêng",
                "source": mask_email(source.email),
                "target": mask_email(target.email),
            })
            self._thread = threading.Thread(
                target=self._worker,
                args=(source, target, options, choose),
                name="chat-backup-worker",
                daemon=True,
            )
            self._thread.start()
        return self.status()

    def _worker(self, source, target, options: RunOptions, choose: bool = False) -> None:
        store = BackupStore(self.data_dir)
        try:
            counts = run_backup(
                source, target, store, self._stop_event, self._event, options,
                select_conversations=self._choose_conversations if choose else None,
            )
            all_failed = bool(counts["total"]) and counts["failed"] == counts["total"] and counts["done"] == 0
            partial_failures = counts["failed"] > 0 and not all_failed
            with self._lock:
                self._state.update(counts)
                self._state.update({
                    "running": False,
                    "stage": "error" if all_failed else ("complete" if counts["total"] else "empty"),
                    "message": (
                        "Không sao chép được cuộc trò chuyện nào; hãy xem lỗi trong checkpoint" if all_failed
                        else f"Đã hoàn tất nhưng có {counts['failed']} lỗi" if partial_failures
                        else "Đã hoàn tất sao chép" if counts["total"]
                        else "Đăng nhập thành công nhưng tài khoản nguồn không có cuộc trò chuyện"
                    ),
                    "current": counts["total"],
                })
        except BackupStopped:
            with self._lock:
                self._state.update({"running": False, "stage": "stopped", "message": "Đã dừng"})
        except Exception as exc:
            with self._lock:
                self._state.update({
                    "running": False,
                    "stage": "error",
                    "message": safe_error(exc)[:500],
                })
        finally:
            store.close()

    def stop(self) -> dict[str, object]:
        self._stop_event.set()
        with self._lock:
            if self._state["running"]:
                self._state.update({"stage": "stopping", "message": "Đang đóng browser an toàn"})
        return self.status()

    def history(self) -> list[dict[str, str]]:
        store = BackupStore(self.data_dir)
        try:
            return store.recent()
        finally:
            store.close()


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(socket_path: Path, data_dir: Path = DATA_DIR):
    controller = JobController(data_dir)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: object, status: int = 200) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _read_json(self, max_bytes: int = MAX_REQUEST_BYTES) -> dict[str, object]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > max_bytes:
                raise ValueError("Dữ liệu gửi lên không hợp lệ")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("JSON phải là object")
            return payload

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/api/health":
                self._json({"ok": True, "module": "chat-backup"})
                return
            if path == "/api/status":
                self._json(controller.status())
                return
            if path == "/api/history":
                self._json({"items": controller.history()})
                return
            if path == "/api/conversations":
                self._json(controller.conversations())
                return
            files = {
                "/": (WEB_ROOT / "index.html", "text/html; charset=utf-8"),
                "/app.js": (WEB_ROOT / "app.js", "text/javascript; charset=utf-8"),
                "/styles.css": (WEB_ROOT / "styles.css", "text/css; charset=utf-8"),
            }
            target = files.get(path)
            if target and target[0].is_file():
                self._send(200, target[0].read_bytes(), target[1])
                return
            self._json({"error": "Not found"}, 404)

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            try:
                if path == "/api/jobs/start":
                    self._json(controller.start(self._read_json()), 202)
                    return
                if path == "/api/jobs/scan":
                    self._json(controller.start(self._read_json(), choose=True), 202)
                    return
                if path == "/api/jobs/selection":
                    self._json(controller.select(self._read_json(512 * 1024)), 202)
                    return
                if path in ("/api/jobs/stop", "/api/browser/windows/close"):
                    state = controller.stop()
                    self._json({**state, "closed": bool(state.get("running"))})
                    return
                self._json({"error": "Not found"}, 404)
            except (CredentialError, ValueError, json.JSONDecodeError) as exc:
                self._json({"error": str(exc)}, 400)
            except RuntimeError as exc:
                self._json({"error": str(exc)}, 409)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    data_dir.mkdir(parents=True, exist_ok=True)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    server = ThreadingUnixHTTPServer(str(socket_path), Handler)
    server.controller = controller
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="ChatGPT shared-link backup module")
    parser.add_argument("--socket", default=str(SOCKET_PATH))
    args = parser.parse_args()
    socket_path = Path(args.socket).expanduser()
    server = create_server(socket_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.controller.stop()
        server.server_close()
        socket_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
