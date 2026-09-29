import io
import json
import unittest
from unittest import mock

import auto_login
import server


class CodexWebTests(unittest.TestCase):
    def test_codex_builds_fresh_pkce_url(self):
        first = auto_login.build_codex_auth_url()
        second = auto_login.build_codex_auth_url()
        self.assertTrue(first[0].startswith('https://auth.openai.com/oauth/authorize?'))
        self.assertIn('redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback', first[0])
        self.assertIn('api.connectors.read', first[0])
        self.assertNotEqual(first[1:], second[1:])

    def test_direct_authorization_fills_forms_completes_oauth_and_never_imports(self):
        class Page:
            def __init__(self):
                self.url = 'about:blank'; self.stage = 'email'; self.filled = []; self.submitted = []
            def goto(self, _url, **_): self.url = 'https://auth.openai.com/log-in'
            def locator(self, selector):
                page = self
                class Field:
                    @property
                    def first(self): return self
                    def is_visible(self):
                        return (page.stage == 'email' and 'type="email"' in selector
                                or page.stage == 'password' and 'type="password"' in selector
                                or page.stage == 'otp' and 'one-time-code' in selector
                                or page.stage == 'consent' and 'Allow' in selector)
                    def fill(self, value): page.filled.append((page.stage, value, page.url))
                    def press(self, _key):
                        page.submitted.append(page.stage)
                        page.stage = {'email':'password', 'password':'otp', 'otp':'consent'}[page.stage]
                        page.url = 'https://auth.openai.com/' + {'password':'log-in/password', 'otp':'mfa-challenge', 'consent':'consent'}[page.stage]
                    def click(self):
                        page.url = 'http://localhost:1455/auth/callback?code=synthetic-code&state=synthetic-state'
                        raise RuntimeError('localhost callback is intentionally not listening')
                return Field()
            def wait_for_timeout(self, _): pass

        page = Page()
        with (
            mock.patch.object(auto_login, 'build_codex_auth_url', return_value=('https://auth.openai.com/oauth/authorize?synthetic', 'verifier', 'synthetic-state')),
            mock.patch.object(auto_login, 'exchange_code') as exchange,
            mock.patch.object(auto_login, 'import_to_9router') as imp,
            mock.patch.object(auto_login, '_session_shutdown_event') as stop,
        ):
            stop.is_set.return_value = False
            self.assertTrue(auto_login.login_codex_authorization(page, 'demo@example.test', 'synthetic', 'JBSWY3DPEHPK3PXP'))
        self.assertEqual(page.submitted, ['email', 'password', 'otp'])
        self.assertEqual(page.filled[1][2], 'https://auth.openai.com/log-in/password')
        exchange.assert_not_called()
        imp.assert_not_called()

    def test_codex_retains_main_tab_and_disables_extra_link_reload(self):
        pw = mock.Mock(); browser = pw.chromium.launch.return_value; context = browser.new_context.return_value
        page = context.new_page.return_value
        with (
            mock.patch.object(auto_login, 'sync_playwright') as start,
            mock.patch.object(auto_login, 'calculate_window_bounds', return_value={'left':0,'top':0,'width':1100,'height':800}),
            mock.patch.object(auto_login, 'apply_browser_window_bounds', return_value=False),
            mock.patch.object(auto_login, 'login_codex_authorization', return_value=True) as login,
            mock.patch.object(auto_login, '_kept_browser_sessions', {}),
            mock.patch('sys.stdout', new_callable=io.StringIO),
        ):
            start.return_value.start.return_value = pw
            result = auto_login.login_web_one_account(1, 1, ('demo@example.test','synthetic',''), codex_web=True,
                                                      open_link='https://example.com/', reload_after=10)
        self.assertTrue(result['codexOpened'])
        login.assert_called_once_with(page, 'demo@example.test', 'synthetic', '', index=1)
        context.new_page.assert_called_once()
        page.reload.assert_not_called(); context.close.assert_not_called(); browser.close.assert_not_called()

    def test_api_accepts_multiple_accounts_and_configured_workers(self):
        handler = server.Handler.__new__(server.Handler); handler.path = '/api/codex-web/start'
        body = json.dumps({'accounts':['demo@example.test|synthetic|', 'second@example.test|synthetic|'], 'workers':2,
                           'openLinkEnabled':True, 'linkUrl':'https://evil.test/', 'reloadEnabled':True}).encode()
        handler.headers = {'Content-Length':str(len(body))}; handler.rfile = io.BytesIO(body); handler._json = mock.Mock()
        with mock.patch.object(server, 'start_web_login', return_value=True) as start:
            handler.do_POST()
        self.assertEqual(start.call_args.kwargs, {'workers':2, 'link_url':'', 'reload_after':None, 'codex_web':True})

    def test_api_rejects_invalid_codex_worker_count(self):
        for workers in (0, 11, 1.5, True):
            handler = server.Handler.__new__(server.Handler); handler.path = '/api/codex-web/start'
            body = json.dumps({'accounts':['demo@example.test|synthetic|'], 'workers':workers}).encode()
            handler.headers = {'Content-Length':str(len(body))}; handler.rfile = io.BytesIO(body); handler._json = mock.Mock()
            with mock.patch.object(server, 'start_web_login') as start:
                handler.do_POST()
            start.assert_not_called(); self.assertEqual(handler._json.call_args.args[1], 400)

    def test_cli_starts_callback_and_dispatches_codex_queue(self):
        with mock.patch.object(server, 'AUTO_LOGIN_EXECUTABLE', 'worker'):
            command = server.build_web_login_command('synthetic.txt', 1, codex_web=True)
        with (
            mock.patch.object(auto_login.sys, 'argv', command),
            mock.patch.object(auto_login.os.path, 'exists', return_value=True),
            mock.patch.object(auto_login, 'parse_accounts', return_value=[('demo@example.test','synthetic','')]),
            mock.patch.object(auto_login, 'run_web_login_queue', return_value=[]) as run,
            mock.patch.object(auto_login, 'start_callback_dispatcher') as callback,
            mock.patch.object(auto_login.threading, 'Thread'),
            mock.patch.object(auto_login, '_kept_browser_sessions', {}),
            mock.patch('builtins.open', mock.mock_open()), mock.patch('sys.stdout', new_callable=io.StringIO),
        ):
            auto_login.main()
        callback.assert_not_called()
        self.assertTrue(run.call_args.kwargs['codex_web'])

    def test_codex_error_is_not_replayed(self):
        job = mock.Mock(return_value={'email':'demo@example.test','status':'error','error':'synthetic'})
        with mock.patch('sys.stdout', new_callable=io.StringIO), mock.patch.object(auto_login, 'emit_event') as events:
            result = auto_login.run_web_login_queue([('demo@example.test','synthetic','')], 1, job=job, codex_web=True)
        job.assert_called_once(); self.assertEqual(result[0]['status'], 'error')
        self.assertEqual([call.args[0] for call in events.call_args_list], ['QUEUED','ERROR'])

    def test_status_is_scoped(self):
        status = server.new_web_login_status(1, 1, running=True)
        status.update(target='codex', results=[{'email':'demo@example.test','status':'success','codexOpened':True}])
        with mock.patch.object(server, '_web_login_status', status):
            self.assertEqual(server.get_web_login_status_response()['results'], [])
            self.assertTrue(server.get_web_login_status_response('codex')['results'][0]['codexOpened'])


if __name__ == '__main__':
    unittest.main()
