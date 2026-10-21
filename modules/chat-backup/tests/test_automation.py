import threading
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from chat_backup.automation import (
    AutomationError,
    BackupStopped,
    RunOptions,
    _cooldown_seconds,
    _share_url_from_clipboard,
    _share_url_from_page,
    _wait_for_cooldown,
    copy_shared_conversation,
    create_share_link,
    delete_new_share_link,
    fetch_conversations,
    login,
    run_backup,
)
from chat_backup.credentials import Credentials
from chat_backup.storage import BackupStore


class FakePage:
    def __init__(self):
        self.calls = []

    def evaluate(self, script, arguments):
        self.calls.append((script, arguments))
        archived = arguments["archived"]
        offset = arguments["offset"]
        if offset:
            return {"items": []}
        if archived:
            return {"items": [{"id": "archived-1", "title": "Archived"}]}
        return {"items": [
            {"id": "active-1", "title": "Active"},
            {"id": "same-id", "title": "Old title"},
        ]}


class AutomationTests(unittest.TestCase):
    def fake_browser(self):
        browser = Mock()
        source_context, target_context = Mock(), Mock()
        source_context.new_page.return_value = Mock()
        target_context.new_page.return_value = Mock()
        browser.new_context.side_effect = [source_context, target_context]
        playwright = Mock()
        playwright.chromium.launch.return_value = browser
        return playwright

    def test_cooldown_is_short_after_success_and_escalates_after_failures(self):
        with patch("chat_backup.automation.random.randint", side_effect=lambda low, _high: low):
            self.assertEqual(_cooldown_seconds(None, 0), 20)
            self.assertEqual(_cooldown_seconds(RuntimeError("temporary"), 1), 45)
            self.assertEqual(_cooldown_seconds(RuntimeError("temporary"), 2), 75)
            self.assertEqual(_cooldown_seconds(RuntimeError("HTTP 429"), 1), 120)

    def test_cooldown_reports_countdown_and_stops_immediately(self):
        stop_event = Mock()
        stop_event.wait.side_effect = [False, True]
        events = []
        with self.assertRaises(BackupStopped):
            _wait_for_cooldown(20, stop_event, lambda stage, value: events.append((stage, value)))
        self.assertEqual(events, [("cooldown", "Đang nghỉ 20 giây trước chat tiếp theo")])
        self.assertEqual(stop_event.wait.call_count, 2)

    def test_conversation_scan_reads_active_and_archived_with_get_fetch(self):
        page = FakePage()
        result = fetch_conversations(page, threading.Event())
        self.assertEqual({item["id"] for item in result}, {"active-1", "same-id", "archived-1"})
        self.assertTrue(all("fetch('/api/auth/session'" in script for script, _ in page.calls))
        self.assertTrue(all("'Authorization': 'Bearer ' + session.accessToken" in script for script, _ in page.calls))
        self.assertTrue(all("headers['ChatGPT-Account-Id'] = accountId" in script for script, _ in page.calls))
        self.assertEqual({call[1]["archived"] for call in page.calls}, {False, True})

    def test_conversation_scan_rejects_changed_response_shape(self):
        page = Mock()
        page.evaluate.return_value = {"unexpected": []}
        with self.assertRaisesRegex(AutomationError, "không hợp lệ"):
            fetch_conversations(page, threading.Event())

    def test_safe_defaults_keep_browser_visible_and_clean_new_links(self):
        options = RunOptions()
        self.assertFalse(options.headless)
        self.assertTrue(options.cleanup_new_links)

    def test_share_url_can_be_read_from_dialog_text(self):
        page = Mock()
        page.evaluate.return_value = "Link công khai: https://chatgpt.com/share/abc-123"
        self.assertEqual(_share_url_from_page(page), "https://chatgpt.com/share/abc-123")

    def test_share_url_can_be_read_from_clipboard_after_share_click(self):
        page = Mock()
        page.evaluate.return_value = "https://chatgpt.com/share/abc-123"
        self.assertEqual(_share_url_from_clipboard(page), "https://chatgpt.com/share/abc-123")

    def test_create_share_link_accepts_chatgpt_auto_copy_flow(self):
        page = Mock()
        stop = threading.Event()
        with (
            patch("chat_backup.automation._share_url_from_page", return_value=""),
            patch("chat_backup.automation._share_url_from_clipboard", return_value="https://chatgpt.com/share/abc-123"),
            patch("chat_backup.automation._click", return_value=True) as click,
        ):
            result = create_share_link(page, "c1", stop)
        self.assertEqual(result, ("https://chatgpt.com/share/abc-123", True))
        click.assert_called_once()

    def test_create_share_link_does_not_mark_existing_dialog_link_for_cleanup(self):
        page = Mock()
        stop = threading.Event()
        with (
            patch("chat_backup.automation._share_url_from_page", side_effect=["", "https://chatgpt.com/share/existing-123"]),
            patch("chat_backup.automation._share_url_from_clipboard", return_value=""),
            patch("chat_backup.automation._click", return_value=True) as click,
        ):
            result = create_share_link(page, "c1", stop)
        self.assertEqual(result, ("https://chatgpt.com/share/existing-123", False))
        click.assert_called_once()

    def test_copy_waits_for_composer_or_continue_together(self):
        for composer_visible in (True, False):
            with self.subTest(composer_visible=composer_visible):
                page = Mock(url="https://chatgpt.com/c/new-target")
                composer, continue_button = Mock(), Mock()
                page.locator.side_effect = [Mock(first=composer), Mock(first=continue_button)]
                composer.is_visible.return_value = composer_visible
                with patch("chat_backup.automation._click", return_value=True):
                    result = copy_shared_conversation(
                        page, "https://chatgpt.com/share/s1", "Synthetic copy", threading.Event(),
                    )
                self.assertEqual(result, "https://chatgpt.com/c/new-target")
                composer.or_.assert_called_once_with(continue_button)
                composer.or_.return_value.first.wait_for.assert_called_once_with(state="visible", timeout=20_000)
                self.assertEqual(continue_button.click.call_count, int(not composer_visible))
                composer.fill.assert_called_once_with("Synthetic copy")

    def test_copy_missing_composer_fails_without_sending(self):
        page = Mock()
        composer, continue_button = Mock(), Mock()
        page.locator.side_effect = [Mock(first=composer), Mock(first=continue_button)]
        composer.or_.return_value.first.wait_for.side_effect = PlaywrightTimeoutError("not ready")
        with self.assertRaisesRegex(AutomationError, "Không tìm thấy ô gửi"):
            copy_shared_conversation(page, "https://chatgpt.com/share/s1", "Synthetic copy", threading.Event())
        composer.fill.assert_not_called()
        continue_button.click.assert_not_called()

    def test_cleanup_reuses_source_page_and_open_dialog_but_recovers_if_navigated(self):
        for url, dialog_open in (("https://chatgpt.com/c/c1", True), ("https://chatgpt.com/c/c1", False), ("about:blank", False)):
            with self.subTest(url=url, dialog_open=dialog_open):
                page = Mock(url=url)
                page.locator.return_value.first.is_visible.return_value = dialog_open
                with patch("chat_backup.automation._click", return_value=True) as click:
                    self.assertTrue(delete_new_share_link(page, "c1", threading.Event()))
                self.assertEqual(page.goto.call_count, int(url == "about:blank"))
                self.assertEqual(click.call_count, 2 if dialog_open else 3)

    def test_completed_checkpoints_need_no_target_login_or_trailing_cooldown(self):
        for all_complete in (True, False):
            with self.subTest(all_complete=all_complete), tempfile.TemporaryDirectory() as directory:
                store = BackupStore(Path(directory))
                source = Credentials("source@example.com", "password", None)
                target = Credentials("target@example.com", "password", None)
                store.save(source.email, target.email, "c2", "Two", "complete")
                if all_complete:
                    store.save(source.email, target.email, "c1", "One", "complete")
                with (
                    patch("chat_backup.automation.sync_playwright", return_value=nullcontext(self.fake_browser())),
                    patch("chat_backup.automation.login") as login_mock,
                    patch("chat_backup.automation.fetch_conversations", return_value=[
                        {"id": "c1", "title": "One"}, {"id": "c2", "title": "Two"},
                    ]),
                    patch("chat_backup.automation.create_share_link", return_value=("https://chatgpt.com/share/s1", False)),
                    patch("chat_backup.automation.copy_shared_conversation", return_value="https://chatgpt.com/c/new") as copy,
                    patch("chat_backup.automation._wait_for_cooldown") as wait,
                ):
                    result = run_backup(source, target, store, threading.Event(), Mock(), RunOptions())
                self.assertEqual(login_mock.call_count, 1 if all_complete else 2)
                self.assertEqual(copy.call_count, 0 if all_complete else 1)
                self.assertEqual(result["skipped"], 2 if all_complete else 1)
                wait.assert_not_called()
                store.close()

    def test_copy_checkpoint_survives_cleanup_error_or_stop(self):
        for cleanup_error in (RuntimeError("cleanup unavailable"), BackupStopped("stopped")):
            with self.subTest(error=type(cleanup_error).__name__), tempfile.TemporaryDirectory() as directory:
                store = BackupStore(Path(directory))
                source = Credentials("source@example.com", "password", None)
                target = Credentials("target@example.com", "password", None)
                with (
                    patch("chat_backup.automation.sync_playwright", return_value=nullcontext(self.fake_browser())),
                    patch("chat_backup.automation.login"),
                    patch("chat_backup.automation.fetch_conversations", return_value=[{"id": "c1", "title": "One"}]),
                    patch("chat_backup.automation.create_share_link", return_value=("https://chatgpt.com/share/s1", True)),
                    patch("chat_backup.automation.copy_shared_conversation", return_value="https://chatgpt.com/c/new"),
                    patch("chat_backup.automation.delete_new_share_link", side_effect=cleanup_error),
                ):
                    if isinstance(cleanup_error, BackupStopped):
                        with self.assertRaises(BackupStopped):
                            run_backup(source, target, store, threading.Event(), Mock(), RunOptions())
                    else:
                        result = run_backup(source, target, store, threading.Event(), Mock(), RunOptions())
                        self.assertEqual(result["done"], 1)
                        self.assertEqual(result["failed"], 0)
                        self.assertEqual(result["cleanup_failed"], 1)
                self.assertTrue(store.is_complete(source.email, target.email, "c1"))
                self.assertEqual(store.recent()[0]["target_url"], "https://chatgpt.com/c/new")
                store.close()

    def test_error_backoff_remains_before_next_attempt_after_skipped_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BackupStore(Path(directory))
            source = Credentials("source@example.com", "password", None)
            target = Credentials("target@example.com", "password", None)
            store.save(source.email, target.email, "c2", "Two", "complete")
            actions = []
            rate_limit = RuntimeError("HTTP 429")

            def share(_page, conversation_id, _stop):
                actions.append(conversation_id)
                if conversation_id == "c1":
                    raise rate_limit
                return "https://chatgpt.com/share/s3", False

            with (
                patch("chat_backup.automation.sync_playwright", return_value=nullcontext(self.fake_browser())),
                patch("chat_backup.automation.login"),
                patch("chat_backup.automation.fetch_conversations", return_value=[
                    {"id": f"c{index}", "title": str(index)} for index in range(1, 4)
                ]),
                patch("chat_backup.automation.create_share_link", side_effect=share),
                patch("chat_backup.automation.copy_shared_conversation", return_value="https://chatgpt.com/c/new"),
                patch("chat_backup.automation._cooldown_seconds", return_value=120) as cooldown,
                patch("chat_backup.automation._wait_for_cooldown", side_effect=lambda *_args: actions.append("wait")),
            ):
                result = run_backup(source, target, store, threading.Event(), Mock(), RunOptions())
            self.assertEqual(actions, ["c1", "wait", "c3"])
            cooldown.assert_called_once_with(rate_limit, 1)
            self.assertEqual((result["done"], result["failed"], result["skipped"]), (1, 1, 1))
            store.close()

    def test_run_uses_two_isolated_contexts_and_checkpoints_completed_copies(self):
        browser = Mock()
        source_context, target_context = Mock(), Mock()
        source_page, target_page = Mock(), Mock()
        source_context.new_page.return_value = source_page
        target_context.new_page.return_value = target_page
        browser.new_context.side_effect = [source_context, target_context]
        playwright = Mock()
        playwright.chromium.launch.return_value = browser
        source = Credentials("source@example.com", "password", None)
        target = Credentials("target@example.com", "password", None)
        events = []

        with tempfile.TemporaryDirectory() as directory:
            store = BackupStore(Path(directory))
            with (
                patch("chat_backup.automation.sync_playwright", return_value=nullcontext(playwright)),
                patch("chat_backup.automation.login") as login,
                patch("chat_backup.automation.fetch_conversations", return_value=[{"id": "c1", "title": "One"}]),
                patch("chat_backup.automation.create_share_link", return_value=("https://chatgpt.com/share/s1", True)),
                patch("chat_backup.automation.copy_shared_conversation", return_value="https://chatgpt.com/c/new"),
                patch("chat_backup.automation.delete_new_share_link", return_value=True),
            ):
                counts = run_backup(
                    source, target, store, threading.Event(), lambda stage, value: events.append((stage, value)), RunOptions(),
                )
            self.assertEqual(counts["done"], 1)
            self.assertTrue(store.is_complete(source.email, target.email, "c1"))
            store.close()

        self.assertEqual(browser.new_context.call_count, 2)
        self.assertEqual(login.call_count, 2)
        self.assertIn(("item_done", "1"), events)
        source_context.close.assert_called_once_with()
        target_context.close.assert_called_once_with()
        browser.close.assert_called_once_with()

    def test_run_waits_only_between_attempted_conversations(self):
        browser = Mock()
        source_context, target_context = Mock(), Mock()
        source_context.new_page.return_value = Mock()
        target_context.new_page.return_value = Mock()
        browser.new_context.side_effect = [source_context, target_context]
        playwright = Mock()
        playwright.chromium.launch.return_value = browser

        with tempfile.TemporaryDirectory() as directory:
            store = BackupStore(Path(directory))
            with (
                patch("chat_backup.automation.sync_playwright", return_value=nullcontext(playwright)),
                patch("chat_backup.automation.login"),
                patch("chat_backup.automation.fetch_conversations", return_value=[
                    {"id": "c1", "title": "One"}, {"id": "c2", "title": "Two"},
                ]),
                patch("chat_backup.automation.create_share_link", return_value=("https://chatgpt.com/share/s1", True)),
                patch("chat_backup.automation.copy_shared_conversation", return_value="https://chatgpt.com/c/new"),
                patch("chat_backup.automation.delete_new_share_link", return_value=True),
                patch("chat_backup.automation._cooldown_seconds", return_value=20),
                patch("chat_backup.automation._wait_for_cooldown") as wait_for_cooldown,
            ):
                result = run_backup(
                    Credentials("source@example.com", "password", None),
                    Credentials("target@example.com", "password", None),
                    store,
                    threading.Event(),
                    Mock(),
                    RunOptions(),
                )
            store.close()

        self.assertEqual(result["done"], 2)
        wait_for_cooldown.assert_called_once()

    def test_selection_precedes_target_login_and_limit_applies_to_chosen_chats_only(self):
        browser = Mock()
        source_context, target_context = Mock(), Mock()
        source_page, target_page = Mock(), Mock()
        source_context.new_page.return_value = source_page
        target_context.new_page.return_value = target_page
        browser.new_context.side_effect = [source_context, target_context]
        playwright = Mock()
        playwright.chromium.launch.return_value = browser
        conversations = [{"id": f"c{index}", "title": str(index)} for index in range(1, 4)]

        with tempfile.TemporaryDirectory() as directory:
            store = BackupStore(Path(directory))
            with (
                patch("chat_backup.automation.sync_playwright", return_value=nullcontext(playwright)),
                patch("chat_backup.automation.login") as login_mock,
                patch("chat_backup.automation.fetch_conversations", return_value=conversations),
                patch("chat_backup.automation.create_share_link", return_value=("https://chatgpt.com/share/s1", True)) as share,
                patch("chat_backup.automation.copy_shared_conversation", return_value="https://chatgpt.com/c/new") as copy,
                patch("chat_backup.automation.delete_new_share_link", return_value=True),
            ):
                def choose(items):
                    self.assertEqual(items, conversations)
                    self.assertEqual(login_mock.call_count, 1, "target login must wait for selection")
                    share.assert_not_called()
                    copy.assert_not_called()
                    return items[1:]

                stop = threading.Event()
                result = run_backup(
                    Credentials("source@example.com", "password", None),
                    Credentials("target@example.com", "password", None),
                    store, stop, Mock(), RunOptions(limit=1), select_conversations=choose,
                )
                self.assertEqual(result["total"], 1)
                share.assert_called_once_with(source_page, "c2", stop)
                self.assertEqual(copy.call_count, 1)
                self.assertEqual(login_mock.call_count, 2)
                self.assertFalse(store.is_complete("source@example.com", "target@example.com", "c1"))
                self.assertTrue(store.is_complete("source@example.com", "target@example.com", "c2"))
                self.assertFalse(store.is_complete("source@example.com", "target@example.com", "c3"))
            store.close()

    def test_login_retries_email_once_when_password_form_is_delayed(self):
        page = Mock(url="https://chatgpt.com/")
        email, retry_email, password = Mock(), Mock(), Mock()
        events = []
        with (
            patch("chat_backup.automation._first_visible", side_effect=[email, None]),
            patch("chat_backup.automation._fill", side_effect=[email, None, retry_email, password]) as fill,
            patch("chat_backup.automation._click", return_value=True),
        ):
            login(
                page,
                Credentials("source@example.com", "password", None),
                threading.Event(),
                lambda stage, value: events.append((stage, value)),
            )

        self.assertEqual(fill.call_count, 4)
        self.assertTrue(any(stage == "email_retry" for stage, _ in events))
        self.assertTrue(any(stage == "authenticated" for stage, _ in events))


if __name__ == "__main__":
    unittest.main()
