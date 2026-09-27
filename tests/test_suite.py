import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import suite


class SuiteManagerTests(unittest.TestCase):
    def test_modules_use_unix_sockets_and_existing_copied_sources(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"SHOPTAIKHOAN_SUITE_DATA_DIR": directory}, clear=False):
            modules = suite.build_modules(ROOT, Path(sys.executable))

        self.assertEqual({module.public_host for module in modules}, {"twofa.localhost", "export.localhost", "browser.localhost"})
        self.assertEqual({module.socket_path.name for module in modules}, {"twofa.sock", "export.sock", "browser.sock"})
        self.assertTrue(all(module.cwd.is_dir() for module in modules))

    def test_packaged_executable_overrides_replace_source_commands(self):
        overrides = {
            "SHOPTAIKHOAN_SUITE_TWOFA_EXECUTABLE": "/app/twofa",
            "SHOPTAIKHOAN_SUITE_EXPORT_EXECUTABLE": "/app/export",
            "SHOPTAIKHOAN_SUITE_BROWSER_EXECUTABLE": "/app/browser",
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {**overrides, "SHOPTAIKHOAN_SUITE_DATA_DIR": directory}, clear=False):
            modules = suite.build_modules(ROOT, Path(sys.executable))
            twofa_socket = str(Path(directory) / "s" / "twofa.sock")

        self.assertEqual(
            [module.command for module in modules],
            [
                ("/app/twofa", "--uds", twofa_socket, "--port", "5050", "--no-browser"),
                ("/app/export",),
                ("/app/browser",),
            ],
        )

    def test_runtime_root_can_live_outside_the_app_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"SHOPTAIKHOAN_SUITE_DATA_DIR": directory}, clear=False):
                modules = suite.build_modules(ROOT, Path(sys.executable))

        self.assertTrue(all(directory in next(value for key, value in module.env.items() if key.endswith("DATA_DIR")) for module in modules))

    def test_start_sets_isolated_runtime_and_disables_child_browsers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module_dir = root / "module"
            module_dir.mkdir()
            module = suite.Module(
                key="demo",
                label="Demo",
                public_host="demo.localhost",
                socket_path=root / "demo.sock",
                cwd=module_dir,
                command=(sys.executable, "server.py"),
                env={"DEMO_DATA_DIR": str(root / "runtime" / "demo")},
            )
            process = Mock()
            process.poll.return_value = None

            with patch("suite.subprocess.Popen", return_value=process) as popen:
                manager = suite.SuiteManager((module,))
                manager.start()

            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["DEMO_DATA_DIR"], str(root / "runtime" / "demo"))
            self.assertEqual(env["SHOPTAIKHOAN_NO_OPEN_BROWSER"], "1")
            self.assertEqual(env["GPT_TOOL_NO_OPEN_BROWSER"], "1")

    def test_status_reports_each_module_without_leaking_probe_body(self):
        module = suite.Module("demo", "Demo", "demo.localhost", ROOT / "demo.sock", ROOT, (sys.executable,), {})
        manager = suite.SuiteManager((module,), probe=lambda _socket: (True, "secret body"))

        self.assertEqual(
            manager.status(),
            {"modules": [{"key": "demo", "label": "Demo", "ready": True}]},
        )


class HubServerTests(unittest.TestCase):
    def test_hub_serves_dashboard_and_status(self):
        module = suite.Module("demo", "Demo", "demo.localhost", ROOT / "demo.sock", ROOT, (sys.executable,), {})
        manager = suite.SuiteManager((module,), probe=lambda _socket: (True, "ok"))
        server = suite.create_hub_server("127.0.0.1", 0, manager, ROOT / "web")
        thread = suite.start_server_thread(server)
        try:
            port = server.server_address[1]
            with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                html = response.read().decode("utf-8")
            with urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as response:
                payload = json.load(response)
            with urlopen(f"http://127.0.0.1:{port}/favicon.svg", timeout=2) as response:
                favicon = response.read().decode("utf-8")
            self.assertIn("Shoptaikhoan Suite", html)
            self.assertIn('href="/favicon.svg"', html)
            self.assertIn("<svg", favicon)
            self.assertTrue(payload["modules"][0]["ready"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_hub_proxies_module_hosts_to_unix_sockets(self):
        class ModuleHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                return

        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "demo.sock"
            backend = socketserver.UnixStreamServer(str(socket_path), ModuleHandler)
            backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
            backend_thread.start()
            module = suite.Module("demo", "Demo", "demo.localhost", socket_path, ROOT, (sys.executable,), {})
            manager = suite.SuiteManager((module,))
            server = suite.create_hub_server("127.0.0.1", 0, manager, ROOT / "web")
            thread = suite.start_server_thread(server)
            try:
                request = suite.Request(f"http://127.0.0.1:{server.server_address[1]}/api/health", headers={"Host": "demo.localhost"})
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(json.load(response), {"ok": True})
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
                backend.shutdown(); backend.server_close(); backend_thread.join(timeout=2)

    def test_hub_streams_each_backend_chunk_without_waiting_for_eof(self):
        class StreamHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for payload, delay in ((b"data: first\n\n", 0.8), (b"data: second\n\n", 0)):
                    self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
                    self.wfile.flush()
                    time.sleep(delay)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

            def log_message(self, _format, *_args):
                return

        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "stream.sock"
            backend = socketserver.UnixStreamServer(str(socket_path), StreamHandler)
            backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
            backend_thread.start()
            module = suite.Module("demo", "Demo", "demo.localhost", socket_path, ROOT, (sys.executable,), {})
            manager = suite.SuiteManager((module,))
            server = suite.create_hub_server("127.0.0.1", 0, manager, ROOT / "web")
            thread = suite.start_server_thread(server)
            try:
                request = suite.Request(f"http://127.0.0.1:{server.server_address[1]}/events", headers={"Host": "demo.localhost"})
                started = time.monotonic()
                with urlopen(request, timeout=2) as response:
                    self.assertEqual(response.readline(), b"data: first\n")
                self.assertLess(time.monotonic() - started, 0.4)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
                backend.shutdown(); backend.server_close(); backend_thread.join(timeout=2)

    def test_shutdown_endpoint_stops_hub(self):
        manager = suite.SuiteManager(())
        server = suite.create_hub_server("127.0.0.1", 0, manager, ROOT / "web")
        thread = suite.start_server_thread(server)
        try:
            request = suite.Request(f"http://127.0.0.1:{server.server_address[1]}/api/shutdown", method="POST")
            with urlopen(request, timeout=2) as response:
                self.assertEqual(json.load(response), {"ok": True})
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
