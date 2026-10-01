"""Passkey handoff tests; no real credentials or WebAuthn enrollment."""
import asyncio
import threading
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

from test_delete_all_chats import _JobRepo, _SettingsRepo, server
from fastapi.testclient import TestClient
from jobs import TwoFAJob, TwoFAJobManager
from service import TwoFAService, TwoFAFlowError
from passkey_service import PasskeyHandoffs, prepare_passkey_url, validate_passkey_url

URL = 'https://auth.openai.com/passkey-enroll?origin_app_name=ChatGPT&mfa_token=synthetic-state'


class PasskeyUrlTests(TestCase):
    def test_exact_allowlist(self):
        self.assertEqual(validate_passkey_url(URL), URL)
        for url in (URL.replace('https:', 'http:'),
                    URL.replace('auth.openai.com', 'auth.openai.com.evil.test'),
                    URL.replace('auth.openai.com', 'evil@auth.openai.com'),
                    URL.replace('auth.openai.com', 'auth.openai.com:444'),
                    URL.replace('/passkey-enroll', '/log-in'),
                    'https://auth.openai.com/passkey-enroll', URL+'\r\n',
                    URL+'&redirect_uri=https://evil.test', URL+'#fragment',
                    URL+'&mfa_token=duplicate', URL.replace('ChatGPT','OtherApp'),
                    URL.replace('synthetic-state', 'bad%0Astate'),
                    'https://auth.openai.com/passkey-enroll?state=old-contract'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_passkey_url(url)

    def test_one_use_expiry_and_capacity(self):
        clock = Mock(return_value=10)
        store = PasskeyHandoffs(clock=clock)
        token = store.issue(URL)
        self.assertNotIn('synthetic-state', token)
        self.assertEqual(store.consume(token), URL)
        with self.assertRaises(KeyError): store.consume(token)
        token = store.issue(URL)
        clock.return_value = 131
        with self.assertRaises(KeyError): store.consume(token)
        for _ in range(32): store.issue(URL)
        with self.assertRaises(ValueError): store.issue(URL)


class PasskeyHttpTests(IsolatedAsyncioTestCase):
    async def test_official_enrollment_route_no_redirect_following(self):
        transport = AsyncMock()
        transport.cookies = Mock()
        transport.__aenter__.return_value = transport
        transport.post.return_value = SimpleNamespace(status_code=200, json=lambda: {'state_token':'synthetic-state'})
        with patch('curl_cffi.requests.AsyncSession', return_value=transport):
            result = await prepare_passkey_url(session_data={
                'accessToken':'synthetic-token', '__cookies':[
                    {'name':'__Secure-next-auth.session-token','value':'synthetic-cookie','domain':'.chatgpt.com','path':'/'},
                    {'name':'auth-only','value':'private','domain':'auth.openai.com'},
                ]})
        self.assertEqual(result, URL)
        self.assertEqual(transport.cookies.set.call_count, 2)
        self.assertIn(
            (('__Secure-next-auth.session-token', 'synthetic-cookie'),
             {'domain': '.chatgpt.com', 'path': '/'}),
            transport.cookies.set.call_args_list,
        )
        self.assertIn(
            (('auth-only', 'private'), {'domain': 'auth.openai.com', 'path': '/'}),
            transport.cookies.set.call_args_list,
        )
        transport.get.assert_not_awaited()
        transport.post.assert_awaited_once()
        args, kwargs = transport.post.call_args
        self.assertEqual(args[0], 'https://chatgpt.com/backend-api/accounts/mfa/user/request_mfa_token_in_house')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer synthetic-token')
        self.assertEqual(kwargs['headers']['Accept'], '*/*')
        self.assertEqual(kwargs['headers']['Origin'], 'https://chatgpt.com')
        self.assertEqual(kwargs['headers']['Referer'], 'https://chatgpt.com/auth/enroll_mfa?factor=passkey')
        self.assertEqual(kwargs['headers']['x-openai-target-path'], '/backend-api/accounts/mfa/user/request_mfa_token_in_house')
        self.assertEqual(kwargs['headers']['x-openai-target-route'], '/backend-api/accounts/mfa/user/request_mfa_token_in_house')
        self.assertEqual(kwargs['headers']['OAI-Language'], 'en-US')
        for header in ('sec-ch-ua', 'sec-ch-ua-mobile', 'sec-ch-ua-platform',
                       'sec-fetch-dest', 'sec-fetch-mode', 'sec-fetch-site'):
            self.assertIn(header, kwargs['headers'])
        self.assertFalse(kwargs['allow_redirects'])

    async def test_unexpected_html_or_redirect_fails_closed(self):
        for status, payload in ((200,{}),(200,{'state_token':42}),(200,{'state_token':'bad\nstate'}),(302,{}),(401,{}),(403,{}),(500,{})):
            transport=AsyncMock(); transport.cookies=Mock(); transport.__aenter__.return_value=transport
            transport.post.return_value=SimpleNamespace(status_code=status,json=lambda:payload,text='private')
            with patch('curl_cffi.requests.AsyncSession',return_value=transport):
                with self.assertRaises(ValueError) as error:
                    await prepare_passkey_url(session_data={'accessToken':'test','__cookies':[{'name':'__Secure-next-auth.session-token','value':'x','domain':'chatgpt.com'}]})
            self.assertNotIn('private',str(error.exception))

    async def test_invalid_session_never_sends_request(self):
        with patch('curl_cffi.requests.AsyncSession') as transport:
            for session in ({}, {'accessToken':'x','__cookies':[]}, {'accessToken':'x','__cookies':[{'name':'x','value':'x','domain':'evil.test'}]}):
                with self.assertRaises(ValueError): await prepare_passkey_url(session_data=session)
            transport.assert_not_called()


class PasskeyServiceTests(IsolatedAsyncioTestCase):
    async def test_only_login_and_handoff_no_account_mutation(self):
        login=AsyncMock(return_value={'accessToken':'synthetic-token'})
        forbidden=AsyncMock(side_effect=AssertionError('unrelated operation'))
        service=TwoFAService(login_fn=login,rotate_fn=forbidden,usage_fn=forbidden,entitlement_fn=forbidden,delete_chats_fn=forbidden,logout_sessions_fn=forbidden)
        with patch('passkey_service.prepare_passkey_url',new=AsyncMock(return_value=URL)) as prepare:
            self.assertEqual(await service.prepare_passkey(email='demo@example.com',password='synthetic',secret='TEST',timeout=30), URL)
        login.assert_awaited_once(); prepare.assert_awaited_once(); forbidden.assert_not_awaited()

    async def test_errors_hide_sensitive_state(self):
        service=TwoFAService(login_fn=AsyncMock(side_effect=RuntimeError('secret-token')),login_attempts=1)
        with self.assertRaises(TwoFAFlowError) as error:
            await service.prepare_passkey(email='demo@example.com',password='synthetic',secret='TEST',timeout=30)
        self.assertNotIn('secret-token',str(error.exception))

    async def test_login_retries_transient_failure_and_error_stays_safe(self):
        login = AsyncMock(side_effect=RuntimeError('secret-token'))
        service = TwoFAService(login_fn=login, retry_delay=0)
        with self.assertRaisesRegex(TwoFAFlowError, 'Đăng nhập') as error:
            await service.prepare_passkey(email='demo@example.com',password='synthetic',secret='TEST',timeout=30)
        self.assertEqual(login.await_count, 3)
        self.assertNotIn('secret-token', str(error.exception))

    async def test_login_retry_can_recover_before_handoff(self):
        login = AsyncMock(side_effect=[RuntimeError('temporary transport failure'), {'accessToken': 'synthetic-token'}])
        service = TwoFAService(login_fn=login, retry_delay=0)
        with patch('passkey_service.prepare_passkey_url', new=AsyncMock(return_value=URL)):
            self.assertEqual(
                await service.prepare_passkey(
                    email='demo@example.com', password='synthetic', secret='TEST', timeout=30,
                ),
                URL,
            )
        self.assertEqual(login.await_count, 2)

    async def test_transient_login_status_retries_and_switches_to_legacy_flow(self):
        calls = []

        async def login(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError('password verify failed: HTTP 403 - upstream response')
            return {'accessToken': 'synthetic-token'}

        service = TwoFAService(login_fn=None, retry_delay=0)
        with patch.object(service, '_resolve_dependencies', return_value=(login, None)), \
             patch('passkey_service.prepare_passkey_url', new=AsyncMock(return_value=URL)):
            self.assertEqual(
                await service.prepare_passkey(
                    email='demo@example.com', password='synthetic', secret='TEST', timeout=30,
                ),
                URL,
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].get('login_flow'), 'legacy')

    async def test_login_failure_does_not_echo_nested_error(self):
        login = AsyncMock(side_effect=TwoFAFlowError(
            'upstream secret-token', error_kind='invalid_credentials', account_state='unknown',
        ))
        service = TwoFAService(login_fn=login, login_attempts=1)
        with self.assertRaises(TwoFAFlowError) as error:
            await service.prepare_passkey(
                email='demo@example.com', password='synthetic', secret='TEST', timeout=30,
            )
        self.assertNotIn('secret-token', str(error.exception))
        self.assertEqual(error.exception.error_kind, 'invalid_credentials')

    async def test_whole_preparation_has_one_deadline(self):
        async def slow_login(**_):
            await asyncio.sleep(0.02)
            return {'accessToken':'synthetic'}
        async def slow_handoff(**_):
            await asyncio.sleep(0.02)
            return URL
        with patch('passkey_service.prepare_passkey_url', side_effect=slow_handoff):
            with self.assertRaisesRegex(TwoFAFlowError, 'quá thời gian'):
                await TwoFAService(login_fn=slow_login).prepare_passkey(
                    email='demo@example.com', password='synthetic', secret='TEST', timeout=0.03)

    async def test_timeout_keeps_account_locked_until_login_thread_stops(self):
        entered, release = threading.Event(), threading.Event()
        def blocking_login():
            entered.set()
            release.wait(2)
            return {'accessToken':'synthetic'}
        async def login(**_):
            return await asyncio.to_thread(blocking_login)
        manager = TwoFAJobManager(_JobRepo(), _SettingsRepo(), service=TwoFAService(login_fn=login))
        manager.settings['twofa.job_timeout'] = 0.01
        job = TwoFAJob(id='live', email='demo@example.com', password='synthetic', secret='TEST',
                      status='success', account_state='live', login_verified=True)
        manager.jobs['live'] = job
        with patch('passkey_service.prepare_passkey_url', new=AsyncMock()) as prepare:
            task = asyncio.create_task(manager.prepare_passkey('live'))
            try:
                await asyncio.to_thread(entered.wait, 1)
                await asyncio.sleep(0.03)
                self.assertFalse(task.done())
                self.assertTrue(job.passkey_preparing)
                with self.assertRaises(ValueError): manager.recheck('live')
            finally:
                release.set()
                with self.assertRaisesRegex(TwoFAFlowError, 'quá thời gian'): await task
            self.assertFalse(job.passkey_preparing)
            prepare.assert_not_awaited()

    async def test_cancel_keeps_account_locked_until_login_thread_stops(self):
        entered, release = threading.Event(), threading.Event()
        def blocking_login():
            entered.set()
            release.wait(2)
            return {'accessToken':'synthetic'}
        async def login(**_):
            return await asyncio.to_thread(blocking_login)
        manager = TwoFAJobManager(_JobRepo(), _SettingsRepo(), service=TwoFAService(login_fn=login))
        job = TwoFAJob(id='live', email='demo@example.com', password='synthetic', secret='TEST',
                      status='success', account_state='live', login_verified=True)
        manager.jobs['live'] = job
        with patch('passkey_service.prepare_passkey_url', new=AsyncMock()) as prepare:
            task = asyncio.create_task(manager.prepare_passkey('live'))
            await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            await asyncio.sleep(0.03)
            self.assertFalse(task.done())
            self.assertTrue(job.passkey_preparing)
            release.set()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertFalse(job.passkey_preparing)
            prepare.assert_not_awaited()


class PasskeyManagerTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo=_JobRepo(); self.service=SimpleNamespace(prepare_passkey=AsyncMock(return_value=URL))
        self.manager=TwoFAJobManager(self.repo,_SettingsRepo(),service=self.service)
        self.job=TwoFAJob(id='live',email='demo@example.com',password='synthetic',secret='TEST',status='success',account_state='live',login_verified=True)
        self.manager.jobs['live']=self.job; self.manager.order.append('live')

    async def test_preserves_job_database_and_output(self):
        before=asdict(self.job); output=self.manager.output()
        self.assertEqual(await self.manager.prepare_passkey('live'),URL)
        self.assertEqual(asdict(self.job),before); self.assertEqual(self.manager.output(),output)
        self.assertEqual(self.repo.updated,[]); self.assertEqual(self.repo.logs,[])
        self.assertNotIn('passkey_preparing',self.manager._state(self.job))

    async def test_rejects_conflicts_and_cleans_up_on_cancel(self):
        duplicate=replace(self.job,id='other',status='running')
        self.manager.jobs['other']=duplicate
        with self.assertRaises(ValueError): await self.manager.prepare_passkey('live')
        duplicate.status='success'
        entered=asyncio.Event()
        async def pending(**_): entered.set(); await asyncio.Event().wait()
        self.service.prepare_passkey.side_effect=pending
        task=asyncio.create_task(self.manager.prepare_passkey('live')); await entered.wait()
        try:
            with self.assertRaises(ValueError): self.manager.recheck('other')
            with self.assertRaises(ValueError): await self.manager.logout_all_sessions('other')
            with self.assertRaises(ValueError): await self.manager.delete_all_chats('other')
            with self.assertRaises(ValueError): await self.manager.clear_async()
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(self.job.passkey_preparing)


class PasskeyApiTests(TestCase):
    def setUp(self):
        self.service=SimpleNamespace(prepare_passkey=AsyncMock(return_value=URL))
        self.manager=TwoFAJobManager(_JobRepo(),_SettingsRepo(),service=self.service)
        self.job=TwoFAJob(id='live',email='demo@example.com',password='synthetic',secret='TEST',status='success',account_state='live',login_verified=True)
        self.manager.jobs['live']=self.job
        self.passwords=SimpleNamespace(jobs={})
        self.patches=[patch.object(server,'manager',self.manager),patch.object(server,'password_manager',self.passwords),patch.object(server,'passkey_handoffs',PasskeyHandoffs())]
        for p in self.patches: p.start()
        self.client=TestClient(server.app); self.headers={'X-Auth-Token':server.auth_token}
    def tearDown(self):
        for p in reversed(self.patches): p.stop()
    def start(self,headers=None,confirm='ADD_PASSKEY'):
        return self.client.post('/api/jobs/live/passkey/start',headers=self.headers if headers is None else headers,json={'confirm':confirm})
    def test_confirmation_auth_and_password_conflict(self):
        self.assertEqual(self.start(headers={}).status_code,401)
        self.assertEqual(self.start(confirm='yes').status_code,422)
        self.passwords.jobs['p']=SimpleNamespace(email=self.job.email,status='running')
        self.assertEqual(self.start().status_code,409)
        self.service.prepare_passkey.assert_not_awaited()
    def test_one_time_safe_handoff_no_success_claim(self):
        response=self.start(); self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertNotIn('synthetic-state',response.text)
        self.assertNotIn('password',response.text)
        link=response.json()['launch_path']
        response=self.client.get(link,follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.headers['location'],URL)
        self.assertEqual(response.headers['referrer-policy'],'no-referrer')
        self.assertEqual(self.client.get(link,follow_redirects=False).status_code,410)
