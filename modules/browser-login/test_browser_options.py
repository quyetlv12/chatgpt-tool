"""Installed-browser selection uses synthetic accounts and local test pages only."""
import io
import json
import unittest
from unittest import mock

import auto_login
import server
import browser_options


class BrowserSelectionTests(unittest.TestCase):
    def test_detection_lists_only_installed_supported_browsers_without_paths(self):
        brave = '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser'
        with mock.patch.object(browser_options.sys, 'platform', 'darwin'), \
             mock.patch.object(browser_options.os.path, 'isfile', side_effect=lambda path: path == brave), \
             mock.patch.object(browser_options.os, 'access', return_value=True):
            self.assertEqual(browser_options.available_browsers(), [
                {'id': 'auto', 'name': 'Tự động (Chrome / Chromium)'},
                {'id': 'brave', 'name': 'Brave'},
            ])
            self.assertEqual(browser_options.browser_launch_options('brave'), {'executable_path': brave})
            with self.assertRaises(ValueError):
                browser_options.browser_launch_options('edge')
        for value in ('/tmp/browser', '--bad', {}, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                browser_options.browser_launch_options(value)

    def test_api_lists_browsers_and_rejects_unavailable_selection_before_start(self):
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/api/web-login/browsers'
        handler._json = mock.Mock()
        with mock.patch.object(server, 'available_browsers', return_value=[{'id': 'brave', 'name': 'Brave'}]):
            handler.do_GET()
        self.assertEqual(handler._json.call_args.args[0]['browsers'][0]['id'], 'brave')
        handler.path = '/api/web-login/start'
        body = json.dumps({'accounts': ['demo@example.test|synthetic|'], 'workers': 1, 'browser': 'missing'}).encode()
        handler.headers = {'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        with mock.patch.object(server, 'start_web_login') as start:
            handler.do_POST()
        start.assert_not_called()
        self.assertEqual(handler._json.call_args.args[1], 400)

    def test_api_threads_selected_browser_through_worker_command(self):
        handler = server.Handler.__new__(server.Handler)
        handler.path = '/api/web-login/start'
        body = json.dumps({'accounts': ['demo@example.test|synthetic|'], 'workers': 1, 'browser': 'brave'}).encode()
        handler.headers = {'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler._json = mock.Mock()
        with (
            mock.patch.object(server, 'browser_launch_options', return_value={}),
            mock.patch.object(server, '_web_login_status', server.new_web_login_status(running=False)),
            mock.patch.object(server, 'persist_web_login_status'),
            mock.patch.object(server.threading, 'Thread') as thread,
        ):
            handler.do_POST()
        self.assertEqual(thread.call_args.kwargs['kwargs']['browser_id'], 'brave')
        self.assertTrue(handler._json.call_args.args[0]['ok'])

    def test_windows_and_linux_resolve_only_known_installed_paths(self):
        with (
            mock.patch.object(browser_options.sys, 'platform', 'win32'),
            mock.patch.dict(browser_options.os.environ, {'PROGRAMFILES': 'C:/Programs'}, clear=True),
            mock.patch.object(browser_options.os.path, 'isfile', return_value=True),
            mock.patch.object(browser_options.os, 'access', return_value=True),
        ):
            self.assertEqual(browser_options.browser_launch_options('brave')['executable_path'],
                             'C:/Programs/BraveSoftware/Brave-Browser/Application/brave.exe')
        with (
            mock.patch.object(browser_options.sys, 'platform', 'linux'),
            mock.patch.object(browser_options.shutil, 'which', side_effect=lambda command: '/usr/bin/brave' if command == 'brave' else None),
            mock.patch.object(browser_options.os.path, 'isfile', return_value=True),
            mock.patch.object(browser_options.os, 'access', return_value=True),
        ):
            self.assertEqual(browser_options.browser_launch_options('brave'), {'executable_path': '/usr/bin/brave'})

    def test_browser_choice_reaches_cli_and_queue_without_changing_codex(self):
        with mock.patch.object(server, 'AUTO_LOGIN_EXECUTABLE', 'worker'):
            command = server.build_web_login_command('synthetic.txt', 2, '', browser_id='brave')
            self.assertEqual(command[-2:], ['--browser', 'brave'])
            self.assertNotIn('--browser', server.build_web_login_command('synthetic.txt', 2, codex_web=True, browser_id='brave'))
        with (
            mock.patch.object(auto_login.sys, 'argv', command),
            mock.patch.object(auto_login.os.path, 'exists', return_value=True),
            mock.patch.object(auto_login, 'browser_launch_options', return_value={}),
            mock.patch.object(auto_login, 'parse_accounts', return_value=[('demo@example.test', 'synthetic', '')]),
            mock.patch.object(auto_login, 'run_web_login_queue', return_value=[]) as run,
            mock.patch.object(auto_login.threading, 'Thread'),
            mock.patch.object(auto_login, '_kept_browser_sessions', {}),
            mock.patch('builtins.open', mock.mock_open()),
            mock.patch('sys.stdout', new_callable=io.StringIO),
        ):
            auto_login.main()
        self.assertEqual(run.call_args.kwargs['browser_id'], 'brave')

    def test_selected_browser_uses_incognito_isolated_context_and_retains_tabs(self):
        pw = mock.Mock()
        with (
            mock.patch.object(auto_login, 'sync_playwright') as start,
            mock.patch.object(auto_login, 'browser_launch_options', return_value={'executable_path': '/synthetic/brave'}),
            mock.patch.object(auto_login, 'apply_browser_window_bounds', return_value=False),
            mock.patch.object(auto_login, 'login_chatgpt_web', return_value=(True, None)),
            mock.patch.object(auto_login, 'verify_personal_account_label', return_value=True),
            mock.patch.object(auto_login, '_kept_browser_sessions', {}),
            mock.patch.object(auto_login, 'emit_event'),
            mock.patch('sys.stdout', new_callable=io.StringIO),
        ):
            start.return_value.start.return_value = pw
            result = auto_login.login_web_one_account(1, 1, ('demo@example.test', 'synthetic', ''), browser_id='brave', open_link='https://example.test/')
        self.assertEqual(result['status'], 'success')
        options = pw.chromium.launch.call_args.kwargs
        self.assertEqual(options['executable_path'], '/synthetic/brave')
        self.assertIn('--incognito', options['args'])
        self.assertNotIn('channel', options)
        browser = pw.chromium.launch.return_value
        browser.new_context.assert_called_once()
        self.assertEqual(browser.new_context.return_value.new_page.call_count, 2)
        browser.close.assert_not_called()
        pw.chromium.launch_persistent_context.assert_not_called()

    def test_explicit_browser_failure_does_not_fallback_or_retry_forever(self):
        pw = mock.Mock()
        pw.chromium.launch.side_effect = RuntimeError('synthetic error')
        with (
            mock.patch.object(auto_login, 'sync_playwright') as start,
            mock.patch.object(auto_login, 'browser_launch_options', return_value={'executable_path': '/synthetic/brave'}),
            mock.patch.object(auto_login, 'login_chatgpt_web') as login,
            mock.patch.object(auto_login, 'emit_event'),
            mock.patch('sys.stdout', new_callable=io.StringIO),
        ):
            start.return_value.start.return_value = pw
            result = auto_login.login_web_one_account(1, 1, ('demo@example.test', 'synthetic', ''), browser_id='brave')
        self.assertFalse(result['retryable'])
        pw.chromium.launch.assert_called_once()
        login.assert_not_called()
        with mock.patch.object(auto_login, 'emit_event'):
            job = mock.Mock(return_value=result)
            results = auto_login.run_web_login_queue([('demo@example.test', 'synthetic', '')], 1, job=job)
        self.assertEqual(results, [result])
        job.assert_called_once()


if __name__ == '__main__':
    unittest.main()
