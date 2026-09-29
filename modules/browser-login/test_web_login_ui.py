import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parent


class WebLoginUiStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "index.html").read_text(encoding="utf-8")

    def test_has_explicit_cancel_control(self):
        self.assertIn("Hủy phiên hiện tại", self.html)
        self.assertIn('id="btn-web-stop"', self.html)

    def test_account_editor_is_wider_than_results_and_log_column(self):
        self.assertIn('class="workflow-grid web-login-workflow-grid"', self.html)
        self.assertRegex(
            self.html,
            r"\.web-login-workflow-grid\s*\{\s*grid-template-columns:\s*minmax\(440px,\s*1\.35fr\)\s*minmax\(360px,\s*0\.9fr\)",
        )

    def test_has_pause_and_resume_control(self):
        self.assertIn('id="btn-web-pause"', self.html)
        self.assertIn("/api/web-login/pause", self.html)
        self.assertIn("/api/web-login/resume", self.html)

    def test_conflict_keeps_cancel_path_visible(self):
        self.assertIn("response.status === 409", self.html)
        self.assertIn("showWebLoginCancelState", self.html)

    def test_cancel_resets_session_ui(self):
        self.assertIn("resetWebLoginUi", self.html)
        self.assertIn("preserveRunData", self.html)

    def test_incomplete_stopped_state_keeps_logs_visible(self):
        self.assertIn("data.completed || data.stopped", self.html)

    def test_renders_realtime_worker_activity(self):
        self.assertIn("renderWebLoginActivity", self.html)
        self.assertIn("data.logs", self.html)
        self.assertIn("data.retries", self.html)
        self.assertIn('id="web-login-activity"', self.html)

    def test_has_persistent_account_history_controls(self):
        self.assertIn("/api/web-login/history", self.html)
        self.assertIn("loadWebAccountHistory", self.html)
        self.assertIn('id="web-account-history"', self.html)
        self.assertIn('id="btn-clear-web-history"', self.html)

    def test_secondary_url_can_be_enabled_or_disabled(self):
        self.assertIn('id="web-login-link-toggle"', self.html)
        self.assertIn("syncWebLoginLinkToggle", self.html)
        self.assertIn("openLinkEnabled: webLinkEnabled", self.html)
        self.assertIn("linkUrl: webLinkEnabled ? $('web-login-link-input').value.trim() : ''", self.html)
        self.assertIn("if (!webLinkEnabled) hideMsg('web-login-msg')", self.html)
        self.assertIn("shoptaikhoan.webLoginLinkEnabled", self.html)

    def test_current_results_can_focus_and_rearrange_browser_windows(self):
        self.assertIn('id="btn-web-rearrange"', self.html)
        self.assertIn("/api/web-login/rearrange", self.html)
        self.assertIn("/api/web-login/focus", self.html)
        self.assertIn("focusWebLoginTab", self.html)
        self.assertIn("webBrowserControlsAvailable", self.html)
        self.assertIn("data.browserControls === true", self.html)
        self.assertIn("Sắp xếp lại tab", self.html)
        self.assertIn("Mở tab", self.html)

    def test_displays_personal_account_verification_in_results_and_history(self):
        self.assertIn("personalAccountVerified", self.html)
        self.assertIn("Đã xác minh Personal account", self.html)
        self.assertIn("Chưa xác minh Personal account", self.html)

    def test_new_run_clears_only_current_session_results_and_tracks_run_id(self):
        self.assertIn("let webLoginRunId = '';", self.html)
        self.assertIn("webLoginRunId = data.runId || '';", self.html)
        self.assertIn("startWebLoginPolling(accounts.length, webLoginRunId)", self.html)
        self.assertIn("if (expectedRunId && data.runId !== expectedRunId) return", self.html)
        self.assertIn("webLoginHistory = Array.isArray(data.results) ? data.results : [];", self.html)
        self.assertIn("Chưa có kết quả phiên này.", self.html)
        self.assertNotIn("webAccountHistory = [];\n          renderWebAccountHistory();\n          webLoginRunId = data.runId", self.html)


if __name__ == "__main__":
    unittest.main()
