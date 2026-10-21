import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from chat_backup.automation import RunOptions
from chat_backup.credentials import Credentials
from chat_backup.server import JobController, create_server


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path):
        super().__init__("localhost", timeout=2)
        self.socket_path = socket_path

    def connect(self):
        import socket

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.socket_path))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.socket_path = root / "chat-backup.sock"
        self.server = create_server(self.socket_path, root / "data")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.controller.stop()
        worker = self.server.controller._thread
        if worker:
            worker.join(timeout=2)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, payload=None):
        connection = UnixHTTPConnection(self.socket_path)
        body = json.dumps(payload).encode() if payload is not None else None
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        result = response.status, json.load(response) if response.length != 0 else {}
        connection.close()
        return result

    def wait_for_stage(self, stage):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.server.controller.status()["stage"] == stage:
                return
            threading.Event().wait(0.01)
        self.fail(f"Job did not reach {stage}: {self.server.controller.status()}")

    def begin_scan(self, items=None):
        items = items if items is not None else [
            {"id": "c1", "title": "One"}, {"id": "c2", "title": "Two"},
            {"id": "c3", "title": "Three"},
        ]
        selections = []

        def fake_run(_source, _target, _store, _stop, _event, _options, *, select_conversations):
            selected = select_conversations(items)
            selections.extend(item["id"] for item in selected)
            return {"total": len(selected), "done": len(selected), "failed": 0, "skipped": 0, "cleanup_failed": 0}

        patched = patch("chat_backup.server.run_backup", side_effect=fake_run)
        patched.start()
        self.addCleanup(patched.stop)
        status, _payload = self.request("POST", "/api/jobs/scan", {
            "source": "source@example.com|password|JBSWY3DPEHPK3PXP",
            "target": "target@example.com|password|JBSWY3DPEHPK3PXP",
        })
        self.assertEqual(status, 202)
        self.wait_for_stage("choosing")
        return self.server.controller.status()["run_id"], selections

    def test_health_and_status_do_not_expose_secrets(self):
        self.assertEqual(self.request("GET", "/api/health"), (200, {"ok": True, "module": "chat-backup"}))
        status, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(payload["running"])
        self.assertNotIn("password", json.dumps(payload).lower())

    def test_start_rejects_same_account_without_starting_browser(self):
        status, payload = self.request("POST", "/api/jobs/start", {
            "source": "same@example.com|password|JBSWY3DPEHPK3PXP",
            "target": "same@example.com|other|JBSWY3DPEHPK3PXP",
        })
        self.assertEqual(status, 400)
        self.assertIn("phải khác nhau", payload["error"])
        self.assertFalse(self.server.controller.status()["running"])

    def test_scan_waits_for_selected_subset_and_preserves_source_order(self):
        run_id, selections = self.begin_scan()
        status, catalog = self.request("GET", "/api/conversations")
        self.assertEqual(status, 200)
        self.assertEqual(catalog["run_id"], run_id)
        self.assertEqual([item["id"] for item in catalog["items"]], ["c1", "c2", "c3"])
        self.assertEqual(selections, [], "scan must not start copying before explicit selection")
        self.assertNotIn("password", json.dumps(catalog))

        status, _payload = self.request("POST", "/api/jobs/selection", {
            "run_id": run_id, "conversation_ids": ["c3", "c2", "c3"],
        })
        self.assertEqual(status, 202)
        self.wait_for_stage("complete")
        self.assertEqual(selections, ["c2", "c3"])
        self.assertEqual(self.server.controller.status()["total"], 2)

    def test_invalid_selections_leave_job_waiting_without_copying(self):
        run_id, selections = self.begin_scan()
        for selected in (None, [], "c1", ["unknown"], [{}], ["c1"] * 10_001):
            with self.subTest(selected_type=type(selected).__name__):
                status, _payload = self.request("POST", "/api/jobs/selection", {
                    "run_id": run_id, "conversation_ids": selected,
                })
                self.assertEqual(status, 400)
                self.assertEqual(self.server.controller.status()["stage"], "choosing")
        self.assertEqual(selections, [])

    def test_selection_rejects_stale_run_and_repeated_submission(self):
        run_id, _selections = self.begin_scan()
        status, _payload = self.request("POST", "/api/jobs/selection", {
            "run_id": "previous-run", "conversation_ids": ["c1"],
        })
        self.assertEqual(status, 409)
        payload = {"run_id": run_id, "conversation_ids": ["c1"]}
        self.assertEqual(self.request("POST", "/api/jobs/selection", payload)[0], 202)
        self.assertEqual(self.request("POST", "/api/jobs/selection", payload)[0], 409)

    def test_stop_interrupts_selection_wait_and_rejects_selection(self):
        run_id, selections = self.begin_scan()
        self.assertEqual(self.request("POST", "/api/jobs/stop")[0], 200)
        self.wait_for_stage("stopped")
        self.assertFalse(self.server.controller.status()["running"])
        self.assertEqual(selections, [])
        self.assertEqual(self.request("POST", "/api/jobs/selection", {
            "run_id": run_id, "conversation_ids": ["c1"],
        })[0], 409)

    def test_selection_accepts_large_catalog_without_start_request_size_limit(self):
        items = [{"id": f"00000000-0000-0000-0000-{index:012d}", "title": "Chat"} for index in range(10_000)]
        run_id, selections = self.begin_scan(items)
        status, _payload = self.request("POST", "/api/jobs/selection", {
            "run_id": run_id, "conversation_ids": [item["id"] for item in items],
        })
        self.assertEqual(status, 202)
        self.wait_for_stage("complete")
        self.assertEqual(len(selections), 10_000)

    def test_legacy_start_does_not_wait_for_selection(self):
        with patch("chat_backup.server.run_backup", return_value={
            "total": 1, "done": 1, "failed": 0, "skipped": 0, "cleanup_failed": 0,
        }) as run:
            self.assertEqual(self.request("POST", "/api/jobs/start", {
                "source": "source@example.com|password|JBSWY3DPEHPK3PXP",
                "target": "target@example.com|password|JBSWY3DPEHPK3PXP",
            })[0], 202)
            self.wait_for_stage("complete")
        self.assertIsNone(run.call_args.kwargs["select_conversations"])

    def test_zero_source_conversations_reports_empty_instead_of_success(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = JobController(Path(directory))
            credential = Credentials("source@example.com", "password", None)
            with patch("chat_backup.server.run_backup", return_value={
                "total": 0, "done": 0, "failed": 0, "skipped": 0, "cleanup_failed": 0,
            }):
                controller._worker(credential, credential, RunOptions())

        state = controller.status()
        self.assertEqual(state["stage"], "empty")
        self.assertIn("không có cuộc trò chuyện", state["message"])

    def test_all_failed_conversations_report_error_instead_of_success(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = JobController(Path(directory))
            credential = Credentials("source@example.com", "password", None)
            with patch("chat_backup.server.run_backup", return_value={
                "total": 2, "done": 0, "failed": 2, "skipped": 0, "cleanup_failed": 0,
            }):
                controller._worker(credential, credential, RunOptions())

        state = controller.status()
        self.assertEqual(state["stage"], "error")
        self.assertIn("Không sao chép được", state["message"])


if __name__ == "__main__":
    unittest.main()
