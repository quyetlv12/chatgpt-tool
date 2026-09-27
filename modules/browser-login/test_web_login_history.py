import json
import io
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import server


class WebLoginHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.history_file = pathlib.Path(self.temp_dir.name) / "web_login_history.json"
        self.file_patch = mock.patch.object(
            server,
            "WEB_LOGIN_HISTORY_FILE",
            str(self.history_file),
        )
        self.file_patch.start()
        self.state_file = pathlib.Path(self.temp_dir.name) / "web_login_state.json"
        self.state_patch = mock.patch.object(
            server,
            "WEB_LOGIN_STATE_FILE",
            str(self.state_file),
        )
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.file_patch.stop()
        self.temp_dir.cleanup()

    def test_history_persists_only_safe_account_result_fields(self):
        saved = server.record_web_login_history({
            "runId": "run-1",
            "index": 1,
            "email": "user@example.com",
            "status": "success",
            "error": "",
            "startedAt": "2026-09-09T10:00:00Z",
            "finishedAt": "2026-09-09T10:00:03Z",
            "durationSeconds": 3,
            "personalAccountVerified": True,
            "password": "must-not-be-written",
            "totp": "JBSWY3DPEHPK3PXP",
            "session": "secret-session-value",
        })

        history = server.load_web_login_history()
        raw_file = self.history_file.read_text(encoding="utf-8")

        self.assertEqual(history, [saved])
        self.assertEqual(saved["email"], "user@example.com")
        self.assertTrue(saved["personalAccountVerified"])
        self.assertNotIn("password", saved)
        self.assertNotIn("totp", saved)
        self.assertNotIn("session", saved)
        self.assertNotIn("must-not-be-written", raw_file)
        self.assertNotIn("JBSWY3DPEHPK3PXP", raw_file)
        self.assertNotIn("secret-session-value", raw_file)
        if server.os.name != "nt":
            self.assertEqual(self.history_file.stat().st_mode & 0o777, 0o600)

    def test_clear_history_removes_all_saved_entries(self):
        server.record_web_login_history({
            "runId": "run-1",
            "index": 1,
            "email": "user@example.com",
            "status": "error",
            "error": "Login failed",
        })

        server.clear_web_login_history()

        self.assertEqual(server.load_web_login_history(), [])
        self.assertEqual(json.loads(self.history_file.read_text(encoding="utf-8")), [])

    def test_invalid_history_file_is_treated_as_empty(self):
        self.history_file.write_text("not-json", encoding="utf-8")

        self.assertEqual(server.load_web_login_history(), [])

    def test_empty_secondary_url_disables_the_extra_tab(self):
        self.assertEqual(server.normalize_web_login_link(""), "")
        self.assertEqual(server.normalize_web_login_link("   "), "")

    def test_disabled_secondary_url_ignores_stale_invalid_text(self):
        self.assertEqual(server.resolve_web_login_link({
            "openLinkEnabled": False,
            "linkUrl": "javascript:stale-value",
        }), "")

    def test_enabled_secondary_url_still_validates_the_value(self):
        self.assertEqual(server.resolve_web_login_link({
            "openLinkEnabled": True,
            "linkUrl": "https://example.com/after-login",
        }), "https://example.com/after-login")
        with self.assertRaises(ValueError):
            server.resolve_web_login_link({
                "openLinkEnabled": True,
                "linkUrl": "javascript:alert(1)",
            })

    def test_disabled_secondary_url_omits_open_link_worker_flag(self):
        disabled = server.build_web_login_command("accounts.txt", 3, "")
        enabled = server.build_web_login_command(
            "accounts.txt",
            3,
            "https://example.com/after-login",
        )

        self.assertNotIn("--open-link", disabled)
        self.assertEqual(enabled[-2:], ["--open-link", "https://example.com/after-login"])

    def test_missing_secondary_url_keeps_legacy_default(self):
        self.assertEqual(
            server.normalize_web_login_link(None),
            "https://chatgpt.com/api/auth/session",
        )

    def test_rejects_invalid_secondary_url(self):
        with self.assertRaises(ValueError):
            server.normalize_web_login_link("javascript:alert(1)")

    def test_run_snapshot_persists_logs_without_credentials_or_link(self):
        status = server.new_web_login_status(
            total=2,
            workers=1,
            link_url="https://example.com/private-invite",
            run_id="run-safe",
        )
        status["results"].append({
            "email": "safe@example.com",
            "status": "success",
            "password": "must-not-persist",
            "totp": "PRIVATE2FA",
            "linkUrl": "https://example.com/private-invite",
        })
        status["logs"].append({
            "sequence": 1,
            "time": "2026-09-09T10:00:00Z",
            "level": "info",
            "email": "safe@example.com",
            "stage": "start",
            "message": "Bắt đầu đăng nhập",
        })

        server.persist_web_login_status(status)
        restored = server.load_web_login_status()
        raw = self.state_file.read_text(encoding="utf-8")

        self.assertEqual(restored["runId"], "run-safe")
        self.assertEqual(restored["logs"][0]["stage"], "start")
        self.assertNotIn("must-not-persist", raw)
        self.assertNotIn("PRIVATE2FA", raw)
        self.assertNotIn("private-invite", raw)

    def test_old_run_finalization_cannot_clobber_new_run(self):
        old = server.new_web_login_status(1, 1, "", run_id="old")
        current = server.new_web_login_status(2, 1, "", run_id="current")
        with server._web_login_lock:
            server._web_login_status = current

        server.finalize_web_login_status(old)

        self.assertIs(server._web_login_status, current)
        self.assertTrue(server._web_login_status["running"])
        self.assertEqual(server._web_login_status["runId"], "current")

    def test_stale_run_cannot_overwrite_current_snapshot(self):
        stale = server.new_web_login_status(1, 1, "", run_id="stale")
        current = server.new_web_login_status(2, 1, "", run_id="current")
        with server._web_login_lock:
            server._web_login_status = current

        server.persist_web_login_status(current, current_only=True)
        self.assertIsNone(server.persist_web_login_status(stale, current_only=True))

        restored = server.load_web_login_status()
        self.assertEqual(restored["runId"], "current")

    def test_restore_marks_unfinished_run_interrupted_and_keeps_logs(self):
        previous = server.get_web_login_status()
        try:
            status = server.new_web_login_status(2, 1, "", run_id="unfinished")
            status["logs"].append({
                "sequence": 1,
                "time": "2026-09-09T10:00:00Z",
                "level": "info",
                "message": "Đang đăng nhập",
                "email": "safe@example.com",
                "stage": "start",
            })
            server.persist_web_login_status(status)

            self.assertTrue(server.restore_web_login_status())

            restored = server.get_web_login_status()
            self.assertFalse(restored["running"])
            self.assertFalse(restored["paused"])
            self.assertTrue(restored["interrupted"])
            self.assertEqual(restored["runId"], "unfinished")
            self.assertTrue(any(log["stage"] == "start" for log in restored["logs"]))
            self.assertTrue(any(log["stage"] == "restored" for log in restored["logs"]))
        finally:
            with server._web_login_lock:
                server._web_login_status = previous

    def test_cannot_start_new_run_while_retained_web_sessions_exist(self):
        previous = server.get_web_login_status()
        try:
            with server._web_login_lock:
                server._web_login_status = server.new_web_login_status(1, 1, "", run_id="retained")
                server._web_login_status["running"] = False
                server._web_login_status["webReady"] = True

            self.assertFalse(server.start_web_login(["safe@example.com|password|"], 1, "https://example.com"))
        finally:
            with server._web_login_lock:
                server._web_login_status = previous

    def test_start_response_state_has_a_fresh_run_id_and_empty_results(self):
        previous = server.get_web_login_status()
        try:
            with server._web_login_lock:
                server._web_login_status = server.new_web_login_status(running=False)
            with mock.patch.object(server, "persist_web_login_status"), mock.patch.object(
                server.threading.Thread,
                "start",
            ):
                self.assertTrue(server.start_web_login(["safe@example.com|password|"], 1, ""))

            status = server.get_web_login_status()
            self.assertTrue(status["runId"])
            self.assertEqual(status["results"], [])
        finally:
            with server._web_login_lock:
                server._web_login_status = previous

    def test_pause_and_resume_signal_current_process_group(self):
        process = mock.Mock()
        process.pid = 321
        previous_status = server.get_web_login_status()
        previous_proc = server._web_login_proc
        try:
            with server._web_login_lock:
                server._web_login_status = server.new_web_login_status(1, 1, "", run_id="pause")
                server._web_login_proc = process
            with mock.patch("server.os.getpgid", return_value=321), mock.patch("server.os.killpg") as killpg:
                self.assertTrue(server.pause_web_login())
                self.assertTrue(server.get_web_login_status()["paused"])
                self.assertTrue(server.resume_web_login())
                self.assertFalse(server.get_web_login_status()["paused"])
            self.assertEqual(killpg.call_count, 2)
        finally:
            with server._web_login_lock:
                server._web_login_status = previous_status
                server._web_login_proc = previous_proc

    def test_browser_control_command_is_written_to_worker_stdin(self):
        process = mock.Mock()
        process.stdin = io.StringIO()
        process.poll.return_value = None
        previous_status = server.get_web_login_status()
        previous_proc = server._web_login_proc
        try:
            with server._web_login_lock:
                server._web_login_status = server.new_web_login_status(2, 2, "", run_id="control")
                server._web_login_proc = process

            self.assertTrue(server.send_web_login_control("focus", index=2))
            self.assertTrue(server.send_web_login_control("rearrange"))

            commands = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
            self.assertEqual(commands, [
                {"action": "focus", "index": 2},
                {"action": "rearrange"},
            ])
        finally:
            with server._web_login_lock:
                server._web_login_status = previous_status
                server._web_login_proc = previous_proc

    def test_browser_control_rejects_invalid_or_inactive_targets(self):
        previous_proc = server._web_login_proc
        try:
            server._web_login_proc = None
            self.assertFalse(server.send_web_login_control("rearrange"))
            with self.assertRaises(ValueError):
                server.send_web_login_control("focus", index=0)
            with self.assertRaises(ValueError):
                server.send_web_login_control("unknown")
        finally:
            server._web_login_proc = previous_proc

    def test_status_advertises_browser_control_capability(self):
        status = server.get_web_login_status_response()

        self.assertTrue(status["browserControls"])

    def test_worker_events_feed_realtime_logs_and_persistent_history(self):
        fake_worker = pathlib.Path(self.temp_dir.name) / "fake_worker.py"
        fake_worker.write_text(
            "\n".join([
                'print("EVENT|QUEUED|safe@example.com|index=1|total=1", flush=True)',
                'print("EVENT|START|safe@example.com|index=1|total=1|attempt=1", flush=True)',
                'print("EVENT|RETRY|safe@example.com|index=1|attempt=1|nextAttempt=2|delay=3|error=temporary timeout", flush=True)',
                'print("EVENT|START|safe@example.com|index=1|total=1|attempt=2", flush=True)',
                'print("EVENT|PHASE|safe@example.com|index=1|stage=password|message=Da gui mat khau", flush=True)',
                'print("EVENT|PHASE|safe@example.com|index=1|stage=personal_account_verified|message=Da xac minh Personal account", flush=True)',
                'print("EVENT|SUCCESS|safe@example.com|index=1|web=yes|link=yes|reloaded=yes|personal=yes", flush=True)',
                'print("WEB_READY|1", flush=True)',
            ]),
            encoding="utf-8",
        )

        with mock.patch.object(
            server,
            "build_auto_login_command",
            return_value=[sys.executable, str(fake_worker)],
        ):
            server._web_login_worker(
                ["safe@example.com|private-password|PRIVATE2FA"],
                workers=1,
                link_url="https://example.com/path",
            )

        status = server.get_web_login_status()
        serialized = json.dumps({
            "logs": status["logs"],
            "history": server.load_web_login_history(),
        })

        self.assertEqual(status["done"], 1)
        self.assertEqual(status["processed"], 1)
        self.assertEqual(status["queued"], 0)
        self.assertEqual(status["retries"], 1)
        self.assertEqual(status["retryCounts"], {"1": 1})
        self.assertTrue(status["completed"])
        self.assertEqual(status["failed"], 0)
        self.assertTrue(status["results"][0]["personalAccountVerified"])
        self.assertTrue(any(log["stage"] == "password" for log in status["logs"]))
        self.assertTrue(any(log["stage"] == "personal_account_verified" for log in status["logs"]))
        self.assertTrue(any(log["stage"] == "retry" for log in status["logs"]))
        self.assertEqual(server.load_web_login_history()[0]["email"], "safe@example.com")
        self.assertTrue(server.load_web_login_history()[0]["personalAccountVerified"])
        self.assertNotIn("private-password", serialized)
        self.assertNotIn("PRIVATE2FA", serialized)


if __name__ == "__main__":
    unittest.main()
