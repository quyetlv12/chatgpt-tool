import threading
import time
import unittest
import queue
import io
from unittest import mock

import auto_login
import server
from auto_login import (
    apply_browser_window_bounds,
    build_web_context_options,
    calculate_window_bounds,
    detect_primary_screen_size,
    emit_event,
    execute_browser_control_command,
    queue_browser_control_command,
    retry_web_email_step,
    run_web_login_queue,
    select_chatgpt_workspace,
    select_country_region,
    verify_personal_account_label,
)


class WebLoginLinkCommandTests(unittest.TestCase):
    def test_toggle_survives_server_command_and_worker_cli(self):
        for enabled, link, expected in (
            (False, 'https://example.com/stale', ''),
            (True, 'https://example.com/after-login', 'https://example.com/after-login'),
            (True, '', ''),
        ):
            with self.subTest(enabled=enabled, link=link):
                url = server.resolve_web_login_link({'openLinkEnabled': enabled, 'linkUrl': link})
                with mock.patch.object(server, 'AUTO_LOGIN_EXECUTABLE', 'synthetic-worker'):
                    command = server.build_web_login_command('synthetic.txt', 3, url)
                with (
                    mock.patch.object(auto_login.sys, 'argv', command),
                    mock.patch.object(auto_login.os.path, 'exists', return_value=True),
                    mock.patch.object(auto_login, 'parse_accounts', return_value=[('demo@example.test', 'synthetic', '')]),
                    mock.patch.object(auto_login, 'run_web_login_queue', return_value=[]) as run,
                    mock.patch.object(auto_login.threading, 'Thread'),
                    mock.patch.object(auto_login, '_kept_browser_sessions', {}),
                    mock.patch('builtins.open', mock.mock_open()),
                    mock.patch('sys.stdout', new_callable=io.StringIO),
                ):
                    auto_login.main()
                self.assertEqual(run.call_args.args[3], expected)

    def test_queue_without_link_does_not_supply_default_url(self):
        job = mock.Mock(return_value={'email': 'demo@example.test', 'status': 'success'})
        with mock.patch('sys.stdout', new_callable=io.StringIO):
            run_web_login_queue([('demo@example.test', 'synthetic', '')], workers=1, job=job)
        self.assertEqual(job.call_args.args[4], '')

    def test_login_opens_secondary_page_only_with_explicit_url(self):
        for link in ('', 'https://example.com/after-login'):
            with self.subTest(link=link):
                playwright = mock.Mock()
                context = playwright.chromium.launch.return_value.new_context.return_value
                with (
                    mock.patch.object(auto_login, 'sync_playwright') as start,
                    mock.patch.object(auto_login, 'calculate_window_bounds', return_value={'left': 0, 'top': 0, 'width': 1100, 'height': 800}),
                    mock.patch.object(auto_login, 'apply_browser_window_bounds', return_value=False),
                    mock.patch.object(auto_login, 'login_chatgpt_web', return_value=(True, '')),
                    mock.patch.object(auto_login, 'verify_personal_account_label', return_value=True),
                    mock.patch.object(auto_login, '_kept_browser_sessions', {}),
                    mock.patch('sys.stdout', new_callable=io.StringIO),
                ):
                    start.return_value.start.return_value = playwright
                    result = auto_login.login_web_one_account(1, 1, ('demo@example.test', 'synthetic', ''), open_link=link)
                self.assertEqual(result['status'], 'success')
                self.assertEqual(context.new_page.call_count, 2 if link else 1)
                self.assertEqual(result['linkOpened'], bool(link))
                self.assertFalse(result['chatgptReloaded'])
                context.new_page.return_value.reload.assert_not_called()
                if link:
                    context.new_page.return_value.bring_to_front.assert_called_once_with()
                else:
                    context.new_page.return_value.bring_to_front.assert_not_called()
                context.new_page.return_value.close.assert_not_called()
                context.close.assert_not_called()
                playwright.chromium.launch.return_value.close.assert_not_called()
                playwright.stop.assert_not_called()
                if link:
                    context.new_page.return_value.goto.assert_called_once_with(link, wait_until='load', timeout=45000)
                else:
                    context.new_page.return_value.goto.assert_not_called()


class RetainedBrowserTests(unittest.TestCase):
    def test_idle_completed_session_does_not_touch_browser(self):
        commands = mock.Mock()
        commands.get.side_effect = queue.Empty
        entry = {'commands': commands, 'page': mock.Mock(), 'context': mock.Mock(), 'browser': mock.Mock()}
        sessions = {'test': entry}
        with (
            mock.patch.object(auto_login, '_session_shutdown_event') as shutdown,
            mock.patch.object(auto_login, '_kept_browser_sessions', sessions),
            mock.patch.object(auto_login, 'execute_browser_control_command') as execute,
            mock.patch.object(auto_login, 'retained_browser_session_is_alive', return_value=True),
        ):
            shutdown.is_set.side_effect = [False, False, True]
            auto_login.service_retained_browser_commands('test', entry)
        self.assertEqual(commands.get.call_count, 2)
        execute.assert_not_called()
        for key in ('page', 'context', 'browser'):
            self.assertEqual(entry[key].mock_calls, [])
        self.assertEqual(sessions, {})

    def test_closed_browser_session_is_removed_and_stops_worker(self):
        commands = mock.Mock()
        entry = {
            'commands': commands,
            'page': mock.Mock(),
            'context': mock.Mock(),
            'browser': mock.Mock(),
        }
        entry['browser'].is_connected.return_value = False
        sessions = {'closed': entry}
        shutdown = threading.Event()
        with (
            mock.patch.object(auto_login, '_session_shutdown_event', shutdown),
            mock.patch.object(auto_login, '_kept_browser_sessions', sessions),
        ):
            auto_login.service_retained_browser_commands('closed', entry)
        self.assertTrue(shutdown.is_set())
        self.assertEqual(sessions, {})
        commands.get.assert_not_called()

    def test_closing_one_of_multiple_sessions_keeps_worker_alive(self):
        sessions = {}
        shutdown = threading.Event()
        first = {'commands': mock.Mock(), 'page': mock.Mock(), 'context': mock.Mock(), 'browser': mock.Mock()}
        second = {'commands': mock.Mock(), 'page': mock.Mock(), 'context': mock.Mock(), 'browser': mock.Mock()}
        first['page'].is_closed.return_value = True
        second['page'].is_closed.return_value = True
        sessions.update({'first': first, 'second': second})
        with (
            mock.patch.object(auto_login, '_session_shutdown_event', shutdown),
            mock.patch.object(auto_login, '_kept_browser_sessions', sessions),
        ):
            auto_login.service_retained_browser_commands('first', first)
            self.assertFalse(shutdown.is_set())
            self.assertEqual(set(sessions), {'second'})
            auto_login.service_retained_browser_commands('second', second)
        self.assertTrue(shutdown.is_set())
        self.assertEqual(sessions, {})


class FakeWorkspaceLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    def is_visible(self):
        return "Personal account" in self.selector

    def click(self):
        self.page.clicked.append(self.selector)


class FakeWorkspacePage:
    def __init__(self):
        self.clicked = []

    def locator(self, selector):
        return FakeWorkspaceLocator(self, selector)


class FakePersonalAccountLocator:
    def __init__(self, page):
        self.page = page
        self.first = self

    def wait_for(self, **kwargs):
        self.page.calls.append(("wait_for", kwargs))
        if self.page.error:
            raise self.page.error

    def inner_text(self, **kwargs):
        self.page.calls.append(("inner_text", kwargs))
        return self.page.label


class FakePersonalAccountPage:
    def __init__(self, label="Personal account", error=None):
        self.label = label
        self.error = error
        self.calls = []

    def locator(self, selector):
        self.calls.append(("locator", selector))
        return FakePersonalAccountLocator(self)

    def wait_for_function(self, expression, **kwargs):
        self.calls.append(("wait_for_function", expression, kwargs))
        if self.error or self.label.strip() != "Personal account":
            raise self.error or TimeoutError("not found")
        return FakeJSHandle(self.calls)


class FakeJSHandle:
    def __init__(self, calls):
        self.calls = calls

    def dispose(self):
        self.calls.append(("dispose",))


class FakePageWithUnrelatedCaption(FakePersonalAccountPage):
    def __init__(self):
        super().__init__(label="Scheduled")

    def wait_for_function(self, expression, **kwargs):
        self.calls.append(("wait_for_function", expression, kwargs))
        return FakeJSHandle(self.calls)


class PersonalAccountVerificationTests(unittest.TestCase):
    def test_accepts_exact_personal_account_label_in_a_visible_div(self):
        playwright = auto_login.sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(
                '<button>Scheduled</button><div style="display:block">Personal account</div>'
            )
            self.assertTrue(verify_personal_account_label(page, timeout=1000))
        finally:
            browser.close()
            playwright.stop()

    def test_accepts_exact_personal_account_label_after_waiting_for_element(self):
        page = FakePersonalAccountPage("  Personal account\n")

        verified = verify_personal_account_label(page, timeout=12000)

        self.assertTrue(verified)
        self.assertEqual(page.calls[0][0], "wait_for_function")
        self.assertIn("text === expected", page.calls[0][1])
        self.assertEqual(page.calls[0][2]["arg"], "Personal account")
        self.assertEqual(page.calls[0][2]["polling"], 250)
        self.assertEqual(page.calls[0][2]["timeout"], 12000)
        self.assertIn(("dispose",), page.calls)

    def test_rejects_a_different_account_label(self):
        page = FakePersonalAccountPage("Business account")

        self.assertFalse(verify_personal_account_label(page))

    def test_returns_false_when_personal_account_element_is_missing(self):
        page = FakePersonalAccountPage(error=TimeoutError("not found"))

        self.assertFalse(verify_personal_account_label(page, timeout=10))

    def test_ignores_unrelated_caption_and_accepts_visible_exact_text(self):
        page = FakePageWithUnrelatedCaption()

        self.assertTrue(verify_personal_account_label(page, timeout=100))
        self.assertEqual(page.calls[0][0], "wait_for_function")


class BrowserWindowLayoutTests(unittest.TestCase):
    def tearDown(self):
        with auto_login._screen_size_lock:
            auto_login._screen_size_cache = None

    def test_detects_only_main_macos_screen_in_multi_display_desktop(self):
        def fake_check_output(args, **kwargs):
            if "-l" in args and "JavaScript" in args:
                return "0,0,2048,1152\n"
            return "-1512, 0, 2048, 1152\n"

        with mock.patch.object(auto_login.sys, "platform", "darwin"), mock.patch.object(
            auto_login.subprocess,
            "check_output",
            side_effect=fake_check_output,
        ):
            self.assertEqual(detect_primary_screen_size(), (2048, 1152))

    def test_one_account_leaves_clear_space_around_menu_bar_and_dock(self):
        bounds = calculate_window_bounds(1, 1, screen_width=2048, screen_height=1152)

        self.assertEqual(bounds, {
            "left": 10,
            "top": 76,
            "width": 2028,
            "height": 960,
        })
        self.assertGreaterEqual(bounds["top"], 70)
        self.assertLessEqual(bounds["top"] + bounds["height"], 1040)

    def test_two_accounts_share_screen_in_two_columns(self):
        first = calculate_window_bounds(1, 2, screen_width=2048, screen_height=1152)
        second = calculate_window_bounds(2, 2, screen_width=2048, screen_height=1152)

        self.assertLessEqual(first["left"] + first["width"], second["left"])
        self.assertEqual(first["top"], second["top"])
        self.assertEqual(first["width"], 1009)
        self.assertEqual(first["height"], 1024)

    def test_three_accounts_share_a_wide_screen_in_three_columns(self):
        bounds = [
            calculate_window_bounds(index, 3, screen_width=3560, screen_height=1152)
            for index in range(1, 4)
        ]

        self.assertEqual([item["left"] for item in bounds], [10, 1193, 2376])
        self.assertTrue(all(item["top"] == 44 for item in bounds))
        self.assertTrue(all(item["width"] == 1173 for item in bounds))
        self.assertTrue(all(item["height"] == 1024 for item in bounds))

    def test_web_context_uses_native_content_area_as_desktop_viewport(self):
        options = build_web_context_options({"width": 877, "height": 507})

        self.assertTrue(options["no_viewport"])
        self.assertNotIn("viewport", options)
        self.assertNotIn("screen", options)
        self.assertNotIn("device_scale_factor", options)
        self.assertFalse(options["is_mobile"])
        self.assertFalse(options["has_touch"])
        self.assertEqual(options["user_agent"], auto_login.DESKTOP_USER_AGENT)

    def test_ten_accounts_use_two_desktop_columns_without_overlap(self):
        bounds = [
            calculate_window_bounds(index, 10, screen_width=2048, screen_height=1152)
            for index in range(1, 11)
        ]

        self.assertEqual(sorted({item["left"] for item in bounds}), [10, 1029])
        self.assertTrue(all(item["width"] == 1009 for item in bounds))
        self.assertTrue(all(item["height"] == 196 for item in bounds))
        self.assertTrue(build_web_context_options(bounds[0])["no_viewport"])

        for offset, first in enumerate(bounds):
            for second in bounds[offset + 1:]:
                overlaps = not (
                    first["left"] + first["width"] <= second["left"]
                    or second["left"] + second["width"] <= first["left"]
                    or first["top"] + first["height"] <= second["top"]
                    or second["top"] + second["height"] <= first["top"]
                )
                self.assertFalse(overlaps, (first, second))

    def test_ten_accounts_on_reference_width_still_use_two_columns(self):
        bounds = [
            calculate_window_bounds(index, 10, screen_width=1760, screen_height=922)
            for index in range(1, 11)
        ]

        self.assertEqual(sorted({item["left"] for item in bounds}), [10, 885])
        self.assertTrue(all(item["width"] == 865 for item in bounds))
        self.assertTrue(all(item["height"] == 150 for item in bounds))
        self.assertTrue(build_web_context_options(bounds[0])["no_viewport"])

    def test_tiles_five_windows_without_overlap_inside_screen(self):
        bounds = [
            calculate_window_bounds(index, 5, screen_width=2048, screen_height=1117)
            for index in range(1, 6)
        ]

        for item in bounds:
            self.assertGreaterEqual(item["left"], 0)
            self.assertGreaterEqual(item["top"], 0)
            self.assertLessEqual(item["left"] + item["width"], 2048)
            self.assertLessEqual(item["top"] + item["height"], 1117)

        for left_index, first in enumerate(bounds):
            for second in bounds[left_index + 1:]:
                overlaps = not (
                    first["left"] + first["width"] <= second["left"]
                    or second["left"] + second["width"] <= first["left"]
                    or first["top"] + first["height"] <= second["top"]
                    or second["top"] + second["height"] <= first["top"]
                )
                self.assertFalse(overlaps, (first, second))

    def test_current_wide_screen_keeps_desktop_width(self):
        bounds = [
            calculate_window_bounds(index, 5, screen_width=3560, screen_height=1152)
            for index in range(1, 6)
        ]

        self.assertTrue(all(item["width"] >= 1100 for item in bounds))

    def test_retry_uses_same_window_slot_for_same_account(self):
        first = calculate_window_bounds(3, 8, screen_width=1920, screen_height=1080)
        retry = calculate_window_bounds(3, 8, screen_width=1920, screen_height=1080)

        self.assertEqual(first, retry)

    def test_applies_window_bounds_through_chrome_devtools(self):
        session = mock.Mock()
        session.send.side_effect = [
            {"windowId": 42, "bounds": {}},
            {},
        ]
        context = mock.Mock()
        context.new_cdp_session.return_value = session
        page = mock.Mock()
        bounds = {"left": 20, "top": 48, "width": 640, "height": 480}

        applied = apply_browser_window_bounds(context, page, bounds)

        self.assertTrue(applied)
        session.send.assert_any_call("Browser.getWindowForTarget")
        session.send.assert_any_call(
            "Browser.setWindowBounds",
            {
                "windowId": 42,
                "bounds": {
                    "left": 20,
                    "top": 48,
                    "width": 640,
                    "height": 480,
                    "windowState": "normal",
                },
            },
        )
        session.detach.assert_called_once()


class BrowserControlTests(unittest.TestCase):
    def setUp(self):
        self.previous_sessions = auto_login._kept_browser_sessions
        auto_login._kept_browser_sessions = {}

    def tearDown(self):
        auto_login._kept_browser_sessions = self.previous_sessions

    def test_focus_command_is_sent_only_to_matching_browser(self):
        first_commands = queue.Queue()
        second_commands = queue.Queue()
        auto_login._kept_browser_sessions.update({
            "first": {"index": 1, "commands": first_commands},
            "second": {"index": 2, "commands": second_commands},
        })

        queued = queue_browser_control_command("focus", index=2)

        self.assertEqual(queued, 1)
        self.assertTrue(first_commands.empty())
        self.assertEqual(second_commands.get_nowait(), {"action": "focus"})

    def test_rearrange_command_recalculates_every_open_browser(self):
        first_commands = queue.Queue()
        second_commands = queue.Queue()
        auto_login._kept_browser_sessions.update({
            "second": {"index": 7, "commands": second_commands},
            "first": {"index": 3, "commands": first_commands},
        })

        queued = queue_browser_control_command(
            "rearrange",
            screen_width=2048,
            screen_height=1152,
        )

        self.assertEqual(queued, 2)
        self.assertEqual(first_commands.get_nowait(), {
            "action": "layout",
            "bounds": calculate_window_bounds(1, 2, 2048, 1152),
        })
        self.assertEqual(second_commands.get_nowait(), {
            "action": "layout",
            "bounds": calculate_window_bounds(2, 2, 2048, 1152),
        })

    def test_rearrange_large_batch_reuses_readable_worker_slots(self):
        command_queues = []
        for index in range(1, 16):
            commands = queue.Queue()
            command_queues.append(commands)
            auto_login._kept_browser_sessions[str(index)] = {
                "index": index,
                "layout_index": ((index - 1) % 2) + 1,
                "layout_total": 2,
                "commands": commands,
            }

        queued = queue_browser_control_command(
            "rearrange",
            screen_width=2048,
            screen_height=1152,
        )

        self.assertEqual(queued, 15)
        bounds = [commands.get_nowait()["bounds"] for commands in command_queues]
        self.assertTrue(all(item["height"] == 1024 for item in bounds))
        self.assertEqual(
            {item["top"] for item in bounds},
            {
                calculate_window_bounds(1, 2, 2048, 1152)["top"],
                calculate_window_bounds(2, 2, 2048, 1152)["top"],
            },
        )

    def test_browser_owner_executes_focus_and_layout_commands(self):
        page = mock.Mock()
        link_page = mock.Mock()
        context = mock.Mock()
        entry = {
            "email": "safe@example.com",
            "index": 1,
            "context": context,
            "page": page,
            "link_page": link_page,
        }
        bounds = {"left": 10, "top": 44, "width": 1009, "height": 196}

        with mock.patch.object(auto_login, "apply_browser_window_bounds", return_value=True) as apply_bounds:
            self.assertTrue(execute_browser_control_command(entry, {"action": "focus"}))
            self.assertTrue(execute_browser_control_command(entry, {"action": "layout", "bounds": bounds}))

        page.bring_to_front.assert_called_once()
        page.set_viewport_size.assert_not_called()
        link_page.set_viewport_size.assert_not_called()
        apply_bounds.assert_called_once_with(context, page, bounds)


class WebLoginQueueTests(unittest.TestCase):
    def test_large_account_list_uses_stable_account_layout_slots(self):
        accounts = [(f"user{index}@example.com", "password", "") for index in range(15)]
        lock = threading.Lock()
        active_slots = set()
        collisions = []
        layouts = []

        def fake_login(
            index,
            total,
            account,
            slow,
            open_link,
            report_terminal,
            attempt,
            layout_index=None,
            layout_total=None,
            reload_after=None,
            codex_web=False,
            desktop_auth_url=None,
        ):
            with lock:
                if layout_index in active_slots:
                    collisions.append(layout_index)
                active_slots.add(layout_index)
                layouts.append((layout_index, layout_total))
            report_terminal({"email": account[0], "status": "success"})
            time.sleep(0.005)
            with lock:
                active_slots.discard(layout_index)
            return {"email": account[0], "status": "success"}

        with mock.patch.object(auto_login, "login_web_one_account", side_effect=fake_login):
            results = run_web_login_queue(accounts, workers=2)

        self.assertEqual(len(results), 15)
        self.assertFalse(collisions)
        self.assertEqual({layout[0] for layout in layouts}, set(range(1, 16)))
        self.assertTrue(all(layout[1] == 15 for layout in layouts))

    def test_runs_every_account_with_at_most_requested_concurrency(self):
        accounts = [(f"user{index}@example.com", "password", "") for index in range(8)]
        lock = threading.Lock()
        active = 0
        max_active = 0
        processed = []
        thread_names = set()

        def fake_login(index, total, account, slow, open_link, report_terminal, attempt):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
                thread_names.add(threading.current_thread().name)
            time.sleep(0.02)
            with lock:
                processed.append((index, total, account[0]))
                active -= 1
            return {"email": account[0], "status": "success"}

        results = run_web_login_queue(accounts, workers=5, job=fake_login)

        self.assertEqual(max_active, 5)
        self.assertEqual(len(thread_names), 8)
        self.assertEqual(len(results), 8)
        self.assertEqual(sorted(item[0] for item in processed), list(range(1, 9)))
        self.assertTrue(all(item[1] == 8 for item in processed))
        self.assertEqual(
            sorted(result["email"] for result in results),
            sorted(account[0] for account in accounts),
        )

    def test_failure_releases_slot_and_remaining_accounts_continue(self):
        accounts = [(f"user{index}@example.com", "password", "") for index in range(8)]
        processed = []
        lock = threading.Lock()

        attempts = {}

        def fake_login(index, total, account, slow, open_link, report_terminal, attempt):
            with lock:
                processed.append(account[0])
                attempts[index] = attempts.get(index, 0) + 1
            if index == 2 and attempt == 1:
                return {
                    "email": account[0],
                    "status": "error",
                    "error": "simulated tab failure",
                }
            time.sleep(0.01)
            return {"email": account[0], "status": "success"}

        results = run_web_login_queue(accounts, workers=5, job=fake_login, retry_delay=0)

        self.assertEqual(len(processed), 9)
        self.assertEqual(len(results), 8)
        self.assertEqual(attempts[2], 2)
        self.assertTrue(all(result["status"] == "success" for result in results))

    def test_failed_account_is_requeued_after_unstarted_accounts(self):
        accounts = [
            ("first@example.com", "password", ""),
            ("second@example.com", "password", ""),
        ]
        order = []

        def fake_login(index, total, account, slow, open_link, report_terminal, attempt):
            order.append((account[0], attempt))
            if account[0] == "first@example.com" and attempt == 1:
                return {"email": account[0], "status": "error", "error": "temporary"}
            return {"email": account[0], "status": "success"}

        results = run_web_login_queue(accounts, workers=1, job=fake_login, retry_delay=0)

        self.assertEqual(order, [
            ("first@example.com", 1),
            ("second@example.com", 1),
            ("first@example.com", 2),
        ])
        self.assertTrue(all(result["status"] == "success" for result in results))

    def test_keeps_retrying_same_account_until_success(self):
        account = ("persistent@example.com", "password", "")
        attempts = []

        def fake_login(index, total, current, slow, open_link, report_terminal, attempt):
            attempts.append(attempt)
            if attempt < 21:
                return {"email": current[0], "status": "error", "error": "temporary"}
            return {"email": current[0], "status": "success"}

        with mock.patch("auto_login.emit_event"):
            results = run_web_login_queue(
                [account],
                workers=1,
                job=fake_login,
                retry_delay=0,
            )

        self.assertEqual(attempts, list(range(1, 22)))
        self.assertEqual(results[0]["status"], "success")

    def test_terminal_result_releases_queue_before_session_thread_exits(self):
        accounts = [(f"user{index}@example.com", "password", "") for index in range(8)]
        release_sessions = threading.Event()
        terminal_emails = []
        lock = threading.Lock()

        def fake_retained_login(index, total, account, slow, open_link, report_terminal, attempt):
            result = {"email": account[0], "status": "success"}
            with lock:
                terminal_emails.append(account[0])
            report_terminal(result)
            release_sessions.wait(timeout=1)
            return result

        started_at = time.monotonic()
        results = run_web_login_queue(accounts, workers=5, job=fake_retained_login)
        elapsed = time.monotonic() - started_at
        release_sessions.set()

        self.assertLess(elapsed, 0.5)
        self.assertEqual(len(terminal_emails), 8)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(result["status"] == "success" for result in results))


class WorkspaceSelectionTests(unittest.TestCase):
    def test_prefers_personal_account_when_workspace_chooser_is_visible(self):
        page = FakeWorkspacePage()

        selected = select_chatgpt_workspace(page)

        self.assertEqual(selected, "personal")
        self.assertEqual(len(page.clicked), 1)
        self.assertIn("Personal account", page.clicked[0])


class EventOutputTests(unittest.TestCase):
    def test_event_is_one_atomic_sanitized_line(self):
        with mock.patch("auto_login.os.write") as write:
            emit_event(
                "PHASE",
                "user@example.com",
                stage="password",
                message="line one\nline two|extra",
            )

        payload = write.call_args.args[1].decode("utf-8")
        self.assertEqual(payload.count("\n"), 1)
        self.assertTrue(payload.endswith("\n"))
        self.assertIn("message=line one line two/extra", payload)


class EmailStepRetryTests(unittest.TestCase):
    def test_refills_email_once_when_password_page_does_not_appear(self):
        email_input = mock.Mock()
        password_input = mock.Mock()
        with mock.patch(
            "auto_login.fill_first_visible",
            side_effect=[email_input, password_input],
        ) as fill, mock.patch("auto_login.click_first_visible", return_value=True) as click:
            found = retry_web_email_step(
                mock.Mock(),
                "user@example.com",
                "private-password",
                index=2,
            )

        self.assertIs(found, password_input)
        self.assertEqual(fill.call_count, 2)
        self.assertEqual(fill.call_args_list[0].args[2], "user@example.com")
        self.assertEqual(fill.call_args_list[1].args[2], "private-password")
        click.assert_called_once()


class CountrySelectionTests(unittest.TestCase):
    def test_selects_cambodia_via_native_select(self):
        page = mock.Mock()
        select_loc = mock.Mock()
        select_loc.first = select_loc
        select_loc.is_visible.return_value = True

        body_loc = mock.Mock()
        body_loc.first = body_loc
        body_loc.inner_text.return_value = "Phone number required"

        def locator_router(selector):
            if selector == "body":
                return body_loc
            return select_loc

        page.locator.side_effect = locator_router

        with mock.patch("time.sleep", return_value=None):
            result = select_country_region(page, "Cambodia")

        self.assertTrue(result)
        select_loc.select_option.assert_called_once_with(label="Cambodia", timeout=1000)

    def test_selects_cambodia_via_dropdown_listbox(self):
        page = mock.Mock()

        body_loc = mock.Mock()
        body_loc.first = body_loc
        body_loc.inner_text.return_value = "Phone number required"

        native_select = mock.Mock()
        native_select.first = native_select
        native_select.is_visible.return_value = False

        dropdown = mock.Mock()
        dropdown.first = dropdown
        dropdown.is_visible.return_value = True
        dropdown.inner_text.return_value = "United States (+1)"

        cambodia_opt = mock.Mock()
        cambodia_opt.first = cambodia_opt
        cambodia_opt.is_visible.return_value = True

        def locator_router(selector):
            res = mock.Mock()
            if selector == "body":
                return body_loc
            if "select" in selector and "combobox" not in selector:
                res.first = native_select
                return res
            if "Cambodia" in selector:
                res.first = cambodia_opt
                res.all.return_value = [cambodia_opt]
                return res
            if "(+" in selector or "combobox" in selector or "menu" in selector or "listbox" in selector:
                res.all.return_value = [dropdown]
                res.first = dropdown
                # wait_for on listbox-role mock must not raise
                dropdown.wait_for = mock.Mock(return_value=None)
                dropdown.is_visible.return_value = True
                dropdown.element_handle.return_value = mock.Mock()
                return res
            res.all.return_value = []
            res.first = mock.Mock(is_visible=lambda: False)
            return res

        page.locator.side_effect = locator_router
        # page.evaluate is used by virtual-scroll strategy; return False so
        # the mock test exercises the Playwright fallback path.
        page.evaluate.return_value = False

        with mock.patch("time.sleep", return_value=None):
            result = select_country_region(page, "Cambodia")

        self.assertTrue(result)
        dropdown.click.assert_called_once()
        cambodia_opt.click.assert_called_once()

    def test_returns_false_when_no_country_selector_present(self):
        page = mock.Mock()
        body_loc = mock.Mock()
        body_loc.first = body_loc
        body_loc.inner_text.return_value = "Normal page without phone prompt"
        page.locator.return_value = body_loc

        with mock.patch("time.sleep", return_value=None):
            result = select_country_region(page, "Cambodia")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
