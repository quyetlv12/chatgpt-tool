"""Reload and Personal retry checks using synthetic accounts and local pages."""
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
            self.assertEqual(thread.call_args.kwargs['kwargs'], {'reload_after': 37, 'codex_web': False, 'browser_id': 'auto'})

    def test_personal_retry_orders_tabs_until_verified_and_stops_safely(self):
        scenarios = [
            ('verified', [False] * 20 + [True], 3, True, '', True, 20),
            ('already_personal', [True], 3, True, '', True, 0),
            ('reload_off', [False], None, True, '', False, 0),
            ('no_secondary', [False], 3, False, '', False, 0),
            ('cancelled', [False], 3, True, 'cancelled', False, 1),
            ('shutdown_after_check', [False], 3, True, 'cancelled', False, 0),
            ('closed_main', [False], 3, True, 'tabs_closed', False, 0),
            ('closed_secondary', [False], 3, True, 'tabs_closed', False, 0),
            ('disconnected', [False], 3, True, 'tabs_closed', False, 0),
            ('closed_during_wait', [False], 3, True, 'tabs_closed', False, 1),
            ('secondary_reload_error', [False], 3, True, 'link_error', False, 1),
            ('main_focus_error', [False], 3, True, 'main_tab_error', False, 1),
            ('main_reload_error', [False], 3, True, 'reload_error', False, 1),
        ]
        for name, checks, delay, has_link, expected_error, verified, retries in scenarios:
            with self.subTest(name=name):
                pw = mock.Mock()
                browser = pw.chromium.launch.return_value
                context = browser.new_context.return_value
                page, extra = mock.Mock(), mock.Mock()
                page.is_closed.return_value = extra.is_closed.return_value = False
                browser.is_connected.return_value = True
                context.new_page.side_effect = [page, extra]
                calls = mock.Mock()
                outcomes = iter(checks)

                def check_personal(_page):
                    if name == 'closed_main':
                        page.is_closed.return_value = True
                    elif name == 'closed_secondary':
                        extra.is_closed.return_value = True
                    elif name == 'disconnected':
                        browser.is_connected.return_value = False
                    elif name == 'shutdown_after_check':
                        shutdown.is_set.return_value = True
                    return next(outcomes)

                def wait(_seconds):
                    if name == 'closed_during_wait' and calls.delay.call_count == 2:
                        extra.is_closed.return_value = True
                    return name == 'cancelled' and calls.delay.call_count == 2

                if name == 'secondary_reload_error':
                    extra.reload.side_effect = TimeoutError('synthetic')
                if name == 'main_focus_error':
                    page.bring_to_front.side_effect = [None, TimeoutError('synthetic')]
                if name == 'main_reload_error':
                    page.reload.side_effect = [None, TimeoutError('synthetic')]
                with (
                    mock.patch.object(auto_login, 'sync_playwright') as start,
                    mock.patch.object(auto_login, 'calculate_window_bounds', return_value={'left':0,'top':0,'width':1100,'height':800}),
                    mock.patch.object(auto_login, 'apply_browser_window_bounds', return_value=False),
                    mock.patch.object(auto_login, 'login_chatgpt_web', return_value=(True, None)) as login,
                    mock.patch.object(auto_login, 'verify_personal_account_label', side_effect=check_personal) as verify,
                    mock.patch.object(auto_login, '_session_shutdown_event') as shutdown,
                    mock.patch.object(auto_login, '_kept_browser_sessions', {}),
                    mock.patch('sys.stdout', new_callable=io.StringIO),
                ):
                    start.return_value.start.return_value = pw
                    shutdown.is_set.return_value = False
                    shutdown.wait.side_effect = wait
                    for label, action in (
                        ('secondary_focus', extra.bring_to_front), ('secondary_reload', extra.reload),
                        ('main_focus', page.bring_to_front), ('delay', shutdown.wait),
                        ('main_reload', page.reload), ('check', verify),
                    ):
                        calls.attach_mock(action, label)
                    result = auto_login.login_web_one_account(
                        1, 1, ('demo@example.test', 'synthetic', ''),
                        open_link='https://example.test/' if has_link else '', reload_after=delay,
                    )
                self.assertEqual(result['status'], 'success')
                self.assertEqual(result['postLoginError'], expected_error)
                self.assertEqual(result['personalAccountVerified'], verified)
                self.assertEqual(verify.call_count, len(checks))
                self.assertEqual(extra.reload.call_count, retries)
                if name == 'verified':
                    self.assertEqual([call[0] for call in calls.mock_calls],
                                     ['main_focus', 'delay', 'main_reload', 'check'] +
                                     ['secondary_focus', 'secondary_reload', 'main_focus',
                                      'delay', 'main_reload', 'check'] * retries)
                    for call in extra.reload.call_args_list + page.reload.call_args_list:
                        self.assertEqual(call, mock.call(wait_until='load', timeout=45000))
                if name == 'closed_during_wait':
                    page.reload.assert_called_once()
                login.assert_called_once()
                self.assertEqual(context.new_page.call_count, 2 if has_link else 1)
                for resource in (page, extra, context, browser):
                    resource.close.assert_not_called()
                pw.stop.assert_not_called()

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

    def test_real_dom_retry_detects_split_personal_caption_and_retains_both_tabs(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        visits = {'main': 0, 'secondary': 0}
        phases = []
        sessions = {}

        class Fixture(BaseHTTPRequestHandler):
            def do_GET(self):
                key = self.path.strip('/')
                if key not in visits:
                    self.send_response(204)
                    self.end_headers()
                    return
                visits[key] += 1
                caption = ('<span style="display:block">Personal</span><span style="display:block">account</span>'
                           if key == 'main' and visits['secondary'] >= 5 else 'Business account')
                if key == 'main' and visits['secondary'] < 5:
                    caption += '<aside><span style="opacity:0">Personal account</span></aside>'
                body = ('<!doctype html><html><body><div>' + caption + '</div></body></html>').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        http = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        url = 'http://127.0.0.1:{}'.format(http.server_port)
        pw = auto_login.sync_playwright().start()
        launch = pw.chromium.launch
        verify = auto_login.verify_personal_account_label
        try:
            with (
                mock.patch.object(auto_login, 'sync_playwright') as start,
                mock.patch.object(pw.chromium, 'launch', side_effect=lambda **kwargs: launch(**{**kwargs, 'headless': True})),
                mock.patch.object(auto_login, 'login_chatgpt_web', side_effect=lambda page, *_args, **_kwargs:
                                  (page.goto(url + '/main', wait_until='load') is not None, None)) as login,
                mock.patch.object(auto_login, 'verify_personal_account_label', side_effect=lambda page: verify(page, timeout=300)),
                mock.patch.object(auto_login, 'apply_browser_window_bounds', return_value=False),
                mock.patch.object(auto_login, 'calculate_window_bounds', return_value={'left':0,'top':0,'width':1100,'height':800}),
                mock.patch.object(auto_login, '_session_shutdown_event', threading.Event()),
                mock.patch.object(auto_login, '_kept_browser_sessions', sessions),
                mock.patch.object(auto_login, 'emit_web_phase', side_effect=lambda _email, _index, stage, _message: phases.append(stage)),
                mock.patch('sys.stdout', new_callable=io.StringIO),
            ):
                start.return_value.start.return_value = pw
                result = auto_login.login_web_one_account(1, 1, ('demo@example.test', 'synthetic', ''),
                                                         open_link=url + '/secondary', reload_after=1)
            self.assertTrue(result['personalAccountVerified'])
            self.assertEqual(result['postLoginError'], '')
            self.assertEqual(visits, {'main': 6, 'secondary': 5})
            self.assertEqual(phases.count('personal_account_retry'), 4)
            self.assertEqual(phases.count('personal_account_verified'), 1)
            login.assert_called_once()
            session = next(iter(sessions.values()))
            self.assertEqual(len(session['context'].pages), 2)
            self.assertTrue(session['browser'].is_connected())
            self.assertTrue(all(not tab.is_closed() for tab in session['context'].pages))
        finally:
            for session in sessions.values():
                session['browser'].close()
            pw.stop()
            http.shutdown()
            http.server_close()


if __name__ == '__main__':
    unittest.main()
