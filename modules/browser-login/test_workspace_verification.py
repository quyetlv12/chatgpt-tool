"""Workspace checks use synthetic responses only; never accept a live invitation."""
import io
import json
import unittest
import tempfile
from pathlib import Path
from unittest import mock

import auto_login
import server


TARGET = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
INVITE = 'https://chatgpt.com/k12-invite?wId=' + TARGET
EMAIL = 'demo@example.test'


class WorkspaceTargetTests(unittest.TestCase):
    def test_invite_target_is_exact_and_invalid_invites_do_not_fall_back_to_personal(self):
        for url in ('', 'https://example.test/', 'https://chatgpt.com/api/auth/session'):
            self.assertFalse(auto_login.workspace_invite_target(url)['required'])
        self.assertEqual(auto_login.workspace_invite_target(INVITE), {'required': True, 'id': TARGET, 'error': ''})
        for suffix in ('', '?wId=bad', '?wId=' + TARGET + '&wId=' + OTHER):
            state = auto_login.workspace_invite_target('https://chatgpt.com/k12-invite' + suffix)
            self.assertTrue(state['required'])
            self.assertEqual(state['error'], 'invalid_invite')
        self.assertEqual(auto_login.workspace_invite_target(INVITE.replace('https:', 'http:'))['error'], 'invalid_invite')
        for port in ('bad', '9000'):
            self.assertEqual(auto_login.workspace_invite_target(INVITE.replace('chatgpt.com', 'chatgpt.com:' + port))['error'], 'invalid_invite')
        self.assertFalse(auto_login.workspace_invite_target(INVITE.replace('chatgpt.com', 'chatgpt.com.example.test'))['required'])

    def test_active_workspace_only_reads_main_tab_routing_not_account_list_or_foreign_requests(self):
        state = {'id': ''}
        for url, method, value, expected in (
            ('https://chatgpt.com/backend-api/conversations?limit=20', 'GET', TARGET, TARGET),
            ('https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27', 'GET', OTHER, TARGET),
            ('https://example.test/backend-api/conversations', 'GET', OTHER, TARGET),
            ('https://chatgpt.com/backend-api/models', 'POST', OTHER, TARGET),
            ('https://chatgpt.com/backend-api/models', 'GET', 'not-an-id', TARGET),
            ('https://chatgpt.com/backend-api/models', 'GET', OTHER, OTHER),
        ):
            request = mock.Mock(url=url, method=method)
            request.header_value.return_value = value
            auto_login.observe_active_workspace(request, state)
            self.assertEqual(state['id'], expected)


class WorkspaceFlowTests(unittest.TestCase):
    def test_membership_controls_completion_without_personal_label_and_keeps_tabs(self):
        for name, checks, link, expected, error in (
            ('joined', [{'verified': True, 'error': ''}], INVITE, True, ''),
            ('delayed', [{'verified': False, 'error': 'not_joined'}, {'verified': True, 'error': ''}], INVITE, True, ''),
            ('denied', [{'verified': False, 'error': 'access_denied'}], INVITE, False, 'workspace_check_failed'),
            ('deactivated', [{'verified': False, 'error': 'workspace_unavailable'}], INVITE, False, 'workspace_check_failed'),
            ('unknown_activity', [{'verified': False, 'error': 'unsupported_response'}], INVITE, False, 'workspace_check_failed'),
            ('invalid', [], 'https://chatgpt.com/k12-invite', False, 'workspace_check_failed'),
        ):
            with self.subTest(name=name):
                pw = mock.Mock()
                browser = pw.chromium.launch.return_value
                context = browser.new_context.return_value
                page, extra = mock.Mock(), mock.Mock()
                context.new_page.side_effect = [page, extra]
                calls = mock.Mock()
                with (
                    mock.patch.object(auto_login, 'sync_playwright') as start,
                    mock.patch.object(auto_login, 'apply_browser_window_bounds', return_value=False),
                    mock.patch.object(auto_login, 'login_chatgpt_web', return_value=(True, None)) as login,
                    mock.patch.object(auto_login, 'verify_personal_account_label', return_value=True) as personal,
                    mock.patch.object(auto_login, 'verify_workspace_membership', side_effect=checks) as verify,
                    mock.patch.object(auto_login, '_session_shutdown_event') as shutdown,
                    mock.patch.object(auto_login, '_kept_browser_sessions', {}),
                    mock.patch.object(auto_login, 'emit_event') as emit,
                    mock.patch('sys.stdout', new_callable=io.StringIO),
                ):
                    start.return_value.start.return_value = pw
                    shutdown.is_set.return_value = False
                    shutdown.wait.return_value = False
                    for label, action in (
                        ('secondary_load', extra.goto), ('main_focus', page.bring_to_front),
                        ('delay', shutdown.wait), ('main_reload', page.reload), ('membership', verify),
                        ('secondary_focus', extra.bring_to_front), ('secondary_reload', extra.reload),
                    ):
                        calls.attach_mock(action, label)
                    result = auto_login.login_web_one_account(
                        1, 1, (EMAIL, 'synthetic-password', ''), open_link=link, reload_after=1,
                    )
                self.assertTrue(result['workspaceCheckRequired'])
                self.assertEqual(result['workspaceVerified'], expected)
                self.assertFalse(result['personalAccountVerified'])
                self.assertEqual(result['postLoginError'], error)
                self.assertEqual(verify.call_count, len(checks))
                personal.assert_not_called()
                login.assert_called_once()
                if checks:
                    self.assertEqual(calls.mock_calls[:5], [
                        mock.call.secondary_load(link, wait_until='load', timeout=45000),
                        mock.call.main_focus(), mock.call.delay(1),
                        mock.call.main_reload(wait_until='load', timeout=45000),
                        mock.call.membership(page, EMAIL, TARGET),
                    ])
                if name == 'delayed':
                    self.assertEqual([call[0] for call in calls.mock_calls[5:]],
                                     ['secondary_focus', 'secondary_reload', 'main_focus', 'delay', 'main_reload', 'membership'])
                for resource in (page, extra, browser, context):
                    resource.close.assert_not_called()
                success = next(call for call in emit.call_args_list if call.args[0] == 'SUCCESS')
                self.assertEqual(success.kwargs['workspace'], 'yes' if expected else 'no')
                self.assertEqual(success.kwargs['personal'], 'no')

    def test_unjoined_workspace_with_reload_off_warns_without_retry(self):
        pw = mock.Mock()
        with (
            mock.patch.object(auto_login, 'sync_playwright') as start,
            mock.patch.object(auto_login, 'apply_browser_window_bounds', return_value=False),
            mock.patch.object(auto_login, 'login_chatgpt_web', return_value=(True, None)),
            mock.patch.object(auto_login, 'verify_workspace_membership', return_value={'verified': False, 'error': 'not_joined'}) as check,
            mock.patch.object(auto_login, '_kept_browser_sessions', {}),
            mock.patch.object(auto_login, 'emit_event'),
            mock.patch('sys.stdout', new_callable=io.StringIO),
        ):
            start.return_value.start.return_value = pw
            result = auto_login.login_web_one_account(1, 1, (EMAIL, 'synthetic', ''), open_link=INVITE)
        self.assertFalse(result['workspaceVerified'])
        check.assert_called_once()
        pw.chromium.launch.return_value.new_context.return_value.new_page.return_value.reload.assert_not_called()


class WorkspaceBrowserChecks(unittest.TestCase):
    def test_real_browser_retry_waits_for_target_and_keeps_both_tabs_with_other_workspace_active(self):
        with auto_login.sync_playwright() as pw:
            native_launch = pw.chromium.launch
            browsers = []
            membership_reads = []

            def route_request(route):
                path = auto_login.urlparse(route.request.url).path
                if path == '/api/auth/session':
                    route.fulfill(json={'user': {'email': EMAIL}, 'accessToken': 'synthetic-token'})
                elif path.startswith('/backend-api/accounts/check/'):
                    membership_reads.append(path)
                    accounts = {'default': {'account': {'account_id': OTHER, 'is_deactivated': False}}}
                    if len(membership_reads) >= 2:
                        accounts[TARGET] = {'account': {'account_id': TARGET, 'is_deactivated': False}}
                    route.fulfill(json={'accounts': accounts})
                elif path.startswith('/backend-api/conversations'):
                    route.fulfill(json={'items': []})
                else:
                    active = TARGET if path == '/k12-invite' else OTHER
                    route.fulfill(content_type='text/html', body=(
                        '<aside>Personal account</aside><script>fetch("/backend-api/conversations", '
                        '{headers:{"ChatGPT-Account-Id":"' + active + '"}})</script>'
                    ))

            def launch(**kwargs):
                kwargs['headless'] = True
                browser = native_launch(**kwargs)
                browsers.append(browser)
                native_context = browser.new_context

                def new_context(**options):
                    context = native_context(**options)
                    context.route('https://chatgpt.com/**', route_request)
                    return context

                browser.new_context = new_context
                return browser

            def login(page, *_args, **_kwargs):
                page.goto('https://chatgpt.com/')
                return True, None

            try:
                with (
                    mock.patch.object(auto_login, 'sync_playwright') as start,
                    mock.patch.object(pw.chromium, 'launch', side_effect=launch),
                    mock.patch.object(auto_login, 'apply_browser_window_bounds', return_value=False),
                    mock.patch.object(auto_login, 'login_chatgpt_web', side_effect=login),
                    mock.patch.object(auto_login, 'verify_personal_account_label') as personal,
                    mock.patch.object(auto_login, '_session_shutdown_event') as shutdown,
                    mock.patch.object(auto_login, '_kept_browser_sessions', {}) as sessions,
                    mock.patch.object(auto_login, 'emit_event'),
                    mock.patch('sys.stdout', new_callable=io.StringIO),
                ):
                    start.return_value.start.return_value = pw
                    shutdown.is_set.return_value = False
                    shutdown.wait.return_value = False
                    result = auto_login.login_web_one_account(1, 1, (EMAIL, 'synthetic', ''), open_link=INVITE, reload_after=1)
                    self.assertTrue(result['workspaceVerified'])
                    self.assertFalse(result['personalAccountVerified'])
                    self.assertEqual(result['activeWorkspaceId'], OTHER)
                    self.assertEqual(len(membership_reads), 2)
                    entry = next(iter(sessions.values()))
                    self.assertFalse(entry['page'].is_closed())
                    self.assertFalse(entry['link_page'].is_closed())
                    personal.assert_not_called()
            finally:
                for browser in browsers:
                    browser.close()

    def test_real_browser_fetch_checks_identity_target_access_and_never_returns_token(self):
        with auto_login.sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            current = {}
            requests = []

            def route_request(route):
                path = auto_login.urlparse(route.request.url).path
                requests.append(path)
                if path == '/api/auth/session':
                    route.fulfill(json=current['session'])
                elif path.startswith('/backend-api/accounts/check/'):
                    self.assertEqual(route.request.header_value('authorization'), 'Bearer synthetic-token')
                    if isinstance(current['accounts'], str):
                        route.fulfill(content_type='application/json', body=current['accounts'])
                    else:
                        route.fulfill(status=current.get('status', 200), json=current['accounts'])
                else:
                    route.fulfill(content_type='text/html', body='<p>Personal account</p><aside>Current: Business</aside>')

            page.route('https://chatgpt.com/**', route_request)
            page.goto('https://chatgpt.com/')
            active = {'account': {'account_id': TARGET, 'is_deactivated': False}}
            cases = [
                ('joined', {'accounts': {TARGET: active}}, 200, EMAIL, True, ''),
                ('different_workspace', {'accounts': {OTHER: {'account': {'account_id': OTHER, 'is_deactivated': False}}}}, 200, EMAIL, False, 'not_joined'),
                ('key_only', {'accounts': {TARGET: {'account': {'account_id': OTHER, 'is_deactivated': False}}}}, 200, EMAIL, False, 'not_joined'),
                ('pending', {'accounts': {TARGET: {'account': dict(active['account'], is_pending=True)}}}, 200, EMAIL, False, 'not_joined'),
                ('deactivated', {'accounts': {TARGET: {'account': dict(active['account'], is_deactivated=True)}}}, 200, EMAIL, False, 'workspace_unavailable'),
                ('record_deactivated', {'accounts': {TARGET: dict(active, is_deactivated=True)}}, 200, EMAIL, False, 'workspace_unavailable'),
                ('account_deactivated_status', {'accounts': {TARGET: {'account': dict(active['account'], status='deactivated')}}}, 200, EMAIL, False, 'workspace_unavailable'),
                ('record_deactivated_status', {'accounts': {TARGET: dict(active, status='DEACTIVATED')}}, 200, EMAIL, False, 'workspace_unavailable'),
                ('record_unknown_flag', {'accounts': {TARGET: dict(active, is_deactivated='false')}}, 200, EMAIL, False, 'unsupported_response'),
                ('record_null_flag', {'accounts': {TARGET: dict(active, is_deactivated=None)}}, 200, EMAIL, False, 'unsupported_response'),
                ('consistent_active_flags', {'accounts': {TARGET: dict(active, is_deactivated=False)}}, 200, EMAIL, True, ''),
                ('missing_access_evidence', {'accounts': {TARGET: {'account': {'account_id': TARGET}}}}, 200, EMAIL, False, 'unsupported_response'),
                ('unknown_shape', {'workspaces': [TARGET]}, 200, EMAIL, False, 'unsupported_response'),
                ('unrecognized_accounts', {'accounts': {TARGET: {'uuid': TARGET}}}, 200, EMAIL, False, 'unsupported_response'),
                ('forbidden', {'accounts': {TARGET: active}}, 403, EMAIL, False, 'access_denied'),
                ('limited', {'accounts': {TARGET: active}}, 429, EMAIL, False, 'rate_limited'),
                ('server_error', {}, 500, EMAIL, False, 'request_failed'),
                ('invalid_json', 'not-json', 200, EMAIL, False, 'unsupported_response'),
                ('wrong_account', {'accounts': {TARGET: active}}, 200, 'other@example.test', False, 'identity_mismatch'),
            ]
            for value in (None, 'false', 'true', 0, 1, [], {}):
                cases.append(('invalid_activity_flag_' + repr(value),
                              {'accounts': {TARGET: {'account': dict(active['account'], is_deactivated=value)}}},
                              200, EMAIL, False, 'unsupported_response'))
            for label, accounts, status, session_email, verified, error in cases:
                with self.subTest(label=label):
                    requests.clear()
                    current.update(session={'user': {'email': session_email}, 'accessToken': 'synthetic-token'}, accounts=accounts, status=status)
                    result = auto_login.verify_workspace_membership(page, EMAIL, TARGET)
                    self.assertEqual(result, {'verified': verified, 'error': error})
                    self.assertNotIn('synthetic-token', json.dumps(result))
                    if label == 'wrong_account':
                        self.assertEqual(requests, ['/api/auth/session'])
            page.goto('about:blank')
            self.assertEqual(auto_login.verify_workspace_membership(page, EMAIL, TARGET)['error'], 'wrong_origin')
            browser.close()


class WorkspaceResultTests(unittest.TestCase):
    def test_status_fields_are_allowlisted_and_legacy_results_remain_readable(self):
        legacy = server._safe_web_login_result({'personalAccountVerified': True})
        self.assertFalse(legacy['workspaceCheckRequired'])
        self.assertTrue(legacy['personalAccountVerified'])
        safe = server._safe_workspace_result({
            'workspaceCheckRequired': True, 'workspaceVerified': True, 'workspaceId': TARGET,
            'activeWorkspaceId': 'synthetic-token', 'workspaceCheckError': 'synthetic-token',
            'token': 'synthetic-token', 'backend_response': {'private': 'synthetic-token'},
        })
        self.assertEqual(safe['workspaceId'], TARGET)
        self.assertEqual(safe['activeWorkspaceId'], '')
        self.assertEqual(safe['workspaceCheckError'], 'unsupported_response')
        self.assertNotIn('synthetic-token', json.dumps(safe))
        self.assertFalse(safe['workspaceVerified'])
        self.assertEqual(server._safe_workspace_result({'workspaceCheckError': ['invalid']})['workspaceCheckError'], 'unsupported_response')

    def test_worker_metadata_survives_status_and_history_without_invite_secrets(self):
        previous_status = server._web_login_status
        try:
            with (
                tempfile.TemporaryDirectory() as directory,
                mock.patch.object(server, 'RUNTIME_DIR', directory),
                mock.patch.object(server, 'WEB_LOGIN_HISTORY_FILE', str(Path(directory) / 'history.json')),
                mock.patch.object(server, 'WEB_LOGIN_STATE_FILE', str(Path(directory) / 'state.json')),
                mock.patch('subprocess.Popen') as launch,
                mock.patch('sys.stdout', new_callable=io.StringIO),
            ):
                for verified in (True, False):
                    proc = mock.Mock(pid=12345, returncode=0)
                    proc.stdout = io.StringIO(
                        f'EVENT|START|{EMAIL}|index=1\n'
                        f'EVENT|SUCCESS|{EMAIL}|index=1|web=yes|personal=yes|workspace_required=yes'
                        f'|workspace={"yes" if verified else "no"}|workspace_id={TARGET}|active_workspace_id={OTHER}'
                        f'|workspace_error={"" if verified else "not_joined"}\n'
                    )
                    launch.return_value = proc
                    server._web_login_worker([EMAIL + '|synthetic-password|synthetic-totp'], link_url=INVITE + '&invite_secret=synthetic-invite-secret')
                    state = server.safe_web_login_status(server.get_web_login_status())
                    result = state['results'][0]
                    self.assertTrue(result['workspaceCheckRequired'])
                    self.assertEqual(result['workspaceVerified'], verified)
                    self.assertEqual(result['activeWorkspaceId'], OTHER)
                    history = server.load_web_login_history()[0]
                    self.assertEqual(history['workspaceVerified'], verified)
                    self.assertEqual(history['workspaceId'], TARGET)
                    level = next(entry['level'] for entry in state['logs'] if entry['stage'] == 'success')
                    self.assertEqual(level, 'success' if verified else 'warning')
                    raw = ''.join(Path(directory, filename).read_text() for filename in ('history.json', 'state.json'))
                    for secret in ('synthetic-password', 'synthetic-totp', 'synthetic-invite-secret'):
                        self.assertNotIn(secret, raw)
        finally:
            server._web_login_status = previous_status


if __name__ == '__main__':
    unittest.main()
