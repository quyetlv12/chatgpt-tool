"""Optional one-shot reload: no real accounts, browsers, or waiting."""
import io
import json
import unittest
from unittest import mock

import auto_login
import server


class WebReloadTests(unittest.TestCase):
    def test_reload_defaults_off_and_validates_enabled_delay(self):
        self.assertIsNone(server.resolve_web_login_reload({}))
        self.assertIsNone(server.resolve_web_login_reload({'reloadEnabled': False, 'reloadSeconds': 'ignored'}))
        self.assertEqual(server.resolve_web_login_reload({'reloadEnabled': True}), 10)
        for seconds in (1, 10, 37, 3600):
            self.assertEqual(server.resolve_web_login_reload({'reloadEnabled': True, 'reloadSeconds': seconds}), seconds)
        for seconds in (0, -1, 3601, 1.5, True, '10', None):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                server.resolve_web_login_reload({'reloadEnabled': True, 'reloadSeconds': seconds})
        for enabled in ('false', 1, None):
            with self.subTest(enabled=enabled), self.assertRaises(ValueError):
                server.resolve_web_login_reload({'reloadEnabled': enabled})

    def test_api_forwards_reload_and_rejects_bad_delay_before_start(self):
        for enabled, seconds, expected in ((False, 10, None), (True, 10, 10), (True, 37, 37), (True, 0, 'invalid')):
            handler = server.Handler.__new__(server.Handler)
            handler.path = '/api/web-login/start'
            payload = json.dumps({'accounts': ['demo@example.test|synthetic|'], 'workers': 1,
                                  'openLinkEnabled': False, 'reloadEnabled': enabled, 'reloadSeconds': seconds}).encode()
            handler.headers = {'Content-Length': str(len(payload))}
            handler.rfile = io.BytesIO(payload)
            handler._json = mock.Mock()
            with mock.patch.object(server, 'start_web_login', return_value=True) as start:
                handler.do_POST()
            if expected == 'invalid':
                start.assert_not_called()
                self.assertEqual(handler._json.call_args.args[1], 400)
            else:
                self.assertEqual(start.call_args.kwargs['reload_after'], expected)

    def test_server_to_cli_preserves_off_default_and_custom_delay(self):
        for delay in (None, 10, 37):
            with mock.patch.object(server, 'AUTO_LOGIN_EXECUTABLE', 'worker'):
                command = server.build_web_login_command('synthetic.txt', 1, '', reload_after=delay)
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
            self.assertEqual(run.call_args.kwargs['reload_after'], delay)

    def test_optional_reload_keeps_tabs_and_returns_from_secondary_before_checking(self):
        for delay, link, failure, cancelled in (
            (None, '', False, False), (None, 'https://example.com/', False, False),
            (10, '', False, False), (37, 'https://example.com/', False, False),
            (10, '', True, False), (10, '', False, True),
        ):
            with self.subTest(delay=delay, link=link, failure=failure, cancelled=cancelled):
                playwright = mock.Mock()
                browser = playwright.chromium.launch.return_value
                context = browser.new_context.return_value
                page, extra = mock.Mock(name="main_page"), mock.Mock(name="secondary_page")
                page.reload.side_effect = TimeoutError('synthetic timeout') if failure else None
                context.new_page.side_effect = [page, extra]
                with (
                    mock.patch.object(auto_login, 'sync_playwright') as start,
                    mock.patch.object(auto_login, 'calculate_window_bounds', return_value={'left': 0, 'top': 0, 'width': 1100, 'height': 800}),
                    mock.patch.object(auto_login, 'apply_browser_window_bounds', return_value=False),
                    mock.patch.object(auto_login, 'login_chatgpt_web', return_value=(True, '')),
                    mock.patch.object(auto_login, 'verify_personal_account_label', return_value=True) as verify,
                    mock.patch.object(auto_login, '_session_shutdown_event') as shutdown,
                    mock.patch.object(auto_login, '_kept_browser_sessions', {}),
                    mock.patch('sys.stdout', new_callable=io.StringIO),
                ):
                    start.return_value.start.return_value = playwright
                    shutdown.wait.return_value = cancelled
                    shutdown.is_set.return_value = False
                    result = auto_login.login_web_one_account(1, 1, ('demo@example.test', 'synthetic', ''),
                                                              open_link=link, reload_after=delay)
                self.assertEqual(result['status'], 'success')
                self.assertEqual(page.reload.call_count, int(delay is not None and not cancelled))
                self.assertEqual(result['chatgptReloaded'], delay is not None and not failure and not cancelled)
                self.assertEqual(verify.call_count, int(not failure and not cancelled))
                if delay is None:
                    shutdown.wait.assert_not_called()
                else:
                    shutdown.wait.assert_called_once_with(delay)
                for tab in (page, extra):
                    tab.close.assert_not_called()
                if link:
                    page.bring_to_front.assert_called_once_with()
                else:
                    page.bring_to_front.assert_not_called()
                extra.reload.assert_not_called()
                extra.bring_to_front.assert_not_called()
                context.close.assert_not_called()
                browser.close.assert_not_called()
                playwright.stop.assert_not_called()

    def test_post_login_flow_orders_secondary_load_main_focus_reload_and_personal_check(self):
        playwright = mock.Mock()
        browser = playwright.chromium.launch.return_value
        context = browser.new_context.return_value
        page, extra = mock.Mock(name="main_page"), mock.Mock(name="secondary_page")
        context.new_page.side_effect = [page, extra]
        phases = []
        calls = mock.Mock()

        def record_phase(_email, _index, stage, _message):
            phases.append(stage)

        with (
            mock.patch.object(auto_login, 'sync_playwright') as start,
            mock.patch.object(auto_login, 'calculate_window_bounds', return_value={'left': 0, 'top': 0, 'width': 1100, 'height': 800}),
            mock.patch.object(auto_login, 'apply_browser_window_bounds', return_value=False),
            mock.patch.object(auto_login, 'login_chatgpt_web', return_value=(True, None)) as login,
            mock.patch.object(auto_login, 'emit_web_phase', side_effect=record_phase),
            mock.patch.object(auto_login, 'verify_personal_account_label', return_value=True) as verify,
            mock.patch.object(auto_login, '_session_shutdown_event') as shutdown,
            mock.patch.object(auto_login, '_kept_browser_sessions', {}),
            mock.patch('sys.stdout', new_callable=io.StringIO),
        ):
            start.return_value.start.return_value = playwright
            shutdown.wait.return_value = False
            shutdown.is_set.return_value = False
            for name, action in (
                ('login', login), ('secondary_load', extra.goto),
                ('main_focus', page.bring_to_front), ('delay', shutdown.wait),
                ('main_reload', page.reload), ('personal_check', verify),
            ):
                calls.attach_mock(action, name)
            result = auto_login.login_web_one_account(
                1,
                1,
                ('demo@example.test', 'synthetic', ''),
                open_link='https://example.test/secondary',
                reload_after=3,
            )

        self.assertEqual(result['status'], 'success')
        self.assertTrue(result['linkOpened'])
        self.assertTrue(result['chatgptReloaded'])
        verify.assert_called_once_with(page)
        self.assertLess(phases.index('open_link'), phases.index('link_ready'))
        self.assertLess(phases.index('link_ready'), phases.index('main_tab_ready'))
        self.assertLess(phases.index('main_tab_ready'), phases.index('reload_wait'))
        self.assertLess(phases.index('reload_wait'), phases.index('reloaded'))
        self.assertLess(phases.index('reloaded'), phases.index('personal_account_check'))
        extra.goto.assert_called_once_with('https://example.test/secondary', wait_until='load', timeout=45000)
        page.bring_to_front.assert_called_once_with()
        page.reload.assert_called_once_with(wait_until='load', timeout=45000)
        self.assertEqual([call[0] for call in calls.mock_calls], [
            'login', 'secondary_load', 'main_focus', 'delay', 'main_reload', 'personal_check',
        ])

    def test_failed_post_login_step_skips_dependencies_but_retains_session(self):
        for failed_step, expected in (
            ('secondary_load', 'link_error'), ('main_focus', 'main_tab_error'),
            ('main_reload', 'reload_error'),
        ):
            with self.subTest(failed_step=failed_step):
                pw = mock.Mock()
                browser = pw.chromium.launch.return_value
                context = browser.new_context.return_value
                page, extra = mock.Mock(), mock.Mock()
                context.new_page.side_effect = [page, extra]
                {'secondary_load': extra.goto, 'main_focus': page.bring_to_front,
                 'main_reload': page.reload}[failed_step].side_effect = TimeoutError('synthetic')
                with (
                    mock.patch.object(auto_login, 'sync_playwright') as start,
                    mock.patch.object(auto_login, 'calculate_window_bounds', return_value={'left':0,'top':0,'width':1100,'height':800}),
                    mock.patch.object(auto_login, 'apply_browser_window_bounds', return_value=False),
                    mock.patch.object(auto_login, 'login_chatgpt_web', return_value=(True, None)),
                    mock.patch.object(auto_login, 'verify_personal_account_label') as verify,
                    mock.patch.object(auto_login, '_session_shutdown_event') as shutdown,
                    mock.patch.object(auto_login, '_kept_browser_sessions', {}),
                    mock.patch('sys.stdout', new_callable=io.StringIO),
                ):
                    start.return_value.start.return_value = pw
                    shutdown.wait.return_value = False
                    shutdown.is_set.return_value = False
                    result = auto_login.login_web_one_account(1, 1, ('demo@example.test','synthetic',''),
                                                             open_link='https://example.test/', reload_after=3)
                self.assertEqual(result['status'], 'success')
                self.assertEqual(result['postLoginError'], expected)
                self.assertEqual(result['linkOpened'], failed_step != 'secondary_load')
                self.assertFalse(result['personalAccountVerified'])
                verify.assert_not_called()
                page.bring_to_front.assert_called_once_with()
                if failed_step != 'main_reload':
                    shutdown.wait.assert_not_called()
                    page.reload.assert_not_called()
                page.close.assert_not_called()
                extra.close.assert_not_called()
                context.close.assert_not_called()
                browser.close.assert_not_called()
                pw.stop.assert_not_called()

    def test_queue_and_background_thread_receive_custom_delay(self):
        accounts = [('demo@example.test', 'synthetic', '')]
        with (
            mock.patch.object(auto_login, 'login_web_one_account', return_value={'email': accounts[0][0], 'status': 'success'}) as login,
            mock.patch('sys.stdout', new_callable=io.StringIO),
        ):
            auto_login.run_web_login_queue(accounts, 1, reload_after=37)
        self.assertEqual(login.call_args.kwargs['reload_after'], 37)
        with (
            mock.patch.object(server, '_web_login_status', server.new_web_login_status(running=False)),
            mock.patch.object(server, 'persist_web_login_status'),
            mock.patch.object(server.threading, 'Thread') as thread,
        ):
            self.assertTrue(server.start_web_login(['demo@example.test|synthetic|'], reload_after=37))
            self.assertEqual(thread.call_args.kwargs['kwargs'], {'reload_after': 37, 'codex_web': False})

    def test_cli_rejects_invalid_delay_before_reading_accounts(self):
        for value in ('0', '-1', '3601', '1.5', 'NaN', None):
            args = ['worker', 'synthetic.txt', '--web-only', '--reload-after']
            if value is not None:
                args.append(value)
            with (
                self.subTest(value=value),
                mock.patch.object(auto_login.sys, 'argv', args),
                mock.patch.object(auto_login, 'parse_accounts') as read,
                mock.patch('sys.stdout', new_callable=io.StringIO),
            ):
                with self.assertRaises(SystemExit) as error:
                    auto_login.main()
                self.assertEqual(error.exception.code, 1)
                read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
