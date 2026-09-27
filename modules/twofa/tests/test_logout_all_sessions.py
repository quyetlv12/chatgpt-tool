"""Network-free logout contract and non-interference checks."""
import asyncio
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from test_delete_all_chats import _JobRepo, _SettingsRepo, server
from fastapi.testclient import TestClient
from jobs import TwoFAJob, TwoFAJobManager
from service import TwoFAService, TwoFAFlowError
from session_phase import SessionError, logout_all_sessions


class LogoutHttpTests(IsolatedAsyncioTestCase):
    async def test_exact_request_no_redirect_or_retry(self):
        transport = AsyncMock()
        transport.__aenter__.return_value = transport
        transport.post.return_value = SimpleNamespace(status_code=204)
        with patch('curl_cffi.requests.AsyncSession', return_value=transport):
            await logout_all_sessions(access_token='test-token', cookies=[{'name': 'session', 'value': 'test-cookie'}])
        transport.post.assert_awaited_once()
        args, kwargs = transport.post.call_args
        self.assertEqual(args, ('https://chatgpt.com/backend-api/accounts/logout_all',))
        self.assertFalse(kwargs['allow_redirects'])
        self.assertNotIn('json', kwargs)
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer test-token')
        self.assertIn('session=test-cookie', kwargs['headers']['Cookie'])

    async def test_invalid_token_no_network(self):
        with patch('curl_cffi.requests.AsyncSession') as transport:
            for token in ('', ' ', 'x\r\ny'):
                with self.assertRaises(SessionError):
                    await logout_all_sessions(access_token=token)
            transport.assert_not_called()

    async def test_failures_are_safe_and_never_retried(self):
        for status in (302, 401, 409, 500):
            transport = AsyncMock()
            transport.__aenter__.return_value = transport
            transport.post.return_value = SimpleNamespace(status_code=status, text='private-token')
            with patch('curl_cffi.requests.AsyncSession', return_value=transport):
                with self.assertRaises(SessionError) as error:
                    await logout_all_sessions(access_token='test-token')
            self.assertNotIn('private-token', str(error.exception))
            transport.post.assert_awaited_once()


class LogoutServiceTests(IsolatedAsyncioTestCase):
    async def test_login_then_logout_once_no_inspection_or_post_login(self):
        login = AsyncMock(return_value={'accessToken': 'test-token', '__cookies': []})
        logout = AsyncMock()
        forbidden = AsyncMock(side_effect=AssertionError('unrelated operation'))
        service = TwoFAService(login_fn=login, logout_sessions_fn=logout, rotate_fn=forbidden,
                               usage_fn=forbidden, entitlement_fn=forbidden, payment_methods_fn=forbidden)
        await service.logout_all_sessions(email='demo@example.com', password='synthetic', secret='TEST', timeout=30, log=lambda _: None)
        login.assert_awaited_once()
        logout.assert_awaited_once()
        forbidden.assert_not_awaited()

    async def test_ambiguous_failure_is_safe_and_not_retried(self):
        logout = AsyncMock(side_effect=TimeoutError('private-token'))
        service = TwoFAService(login_fn=AsyncMock(return_value={'accessToken': 'test-token'}), logout_sessions_fn=logout)
        with self.assertRaises(TwoFAFlowError) as error:
            await service.logout_all_sessions(email='demo@example.com', password='synthetic', secret='TEST', timeout=30, log=lambda _: None)
        self.assertNotIn('private-token', str(error.exception))
        self.assertIn('chưa xác nhận', str(error.exception))
        logout.assert_awaited_once()


class LogoutManagerTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = _JobRepo()
        self.service = SimpleNamespace(logout_all_sessions=AsyncMock())
        self.manager = TwoFAJobManager(self.repo, _SettingsRepo(), service=self.service)
        self.job = TwoFAJob(id='live', email='demo@example.com', password='synthetic', secret='TEST',
                            status='success', account_state='live', login_verified=True)
        self.manager.jobs[self.job.id] = self.job
        self.manager.order.append(self.job.id)

    async def test_no_persistence_or_result_change(self):
        before = asdict(self.job)
        await self.manager.logout_all_sessions('live')
        self.assertEqual(asdict(self.job), before)
        self.assertEqual(self.repo.updated, [])
        self.assertEqual(self.repo.logs, [])
        self.assertNotIn('sessions_logging_out', self.manager._state(self.job))

    async def test_rejects_active_duplicate_and_clear_in_progress(self):
        other = replace(self.job, id='duplicate', status='running')
        self.manager.jobs[other.id] = other
        with self.assertRaises(ValueError):
            await self.manager.logout_all_sessions('live')
        other.status = 'success'
        other.chat_deleting = True
        with self.assertRaises(ValueError):
            await self.manager.logout_all_sessions('live')
        other.chat_deleting = False
        self.manager._clear_in_progress = True
        with self.assertRaises(ValueError):
            await self.manager.logout_all_sessions('live')
        self.service.logout_all_sessions.assert_not_awaited()

    async def test_lock_and_cancel_preserve_other_flows(self):
        entered = asyncio.Event()
        async def pending(**_):
            entered.set()
            await asyncio.Event().wait()
        self.service.logout_all_sessions.side_effect = pending
        duplicate = replace(self.job, id='duplicate', mode='check_only')
        self.manager.jobs[duplicate.id] = duplicate
        task = asyncio.create_task(self.manager.logout_all_sessions('live'))
        await entered.wait()
        try:
            self.assertTrue(self.job.snapshot()['sessions_logging_out'])
            for method in (self.manager.retry, self.manager.recheck, self.manager.enqueue_change_2fa):
                with self.assertRaises(ValueError):
                    method('duplicate')
            for method in (self.manager.refresh_usage, self.manager.delete_all_chats, self.manager.logout_all_sessions):
                with self.assertRaises(ValueError):
                    await method('duplicate')
            with self.assertRaises(ValueError): self.manager.delete('live')
            with self.assertRaises(ValueError): self.manager.clear()
            with self.assertRaises(ValueError): await self.manager.clear_async()
            with self.assertRaises(ValueError): self.manager.add(['DEMO@example.com|synthetic|TEST'])
            self.manager.assert_not_logging_out('another@example.com')
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(self.job.sessions_logging_out)


class LogoutApiTests(TestCase):
    def setUp(self):
        self.repo = _JobRepo()
        self.service = SimpleNamespace(logout_all_sessions=AsyncMock())
        self.manager = TwoFAJobManager(self.repo, _SettingsRepo(), service=self.service)
        self.job = TwoFAJob(id='live', email='demo@example.com', password='synthetic', secret='TEST', status='success', account_state='live')
        self.manager.jobs['live'] = self.job
        self.passwords = SimpleNamespace(jobs={}, add=lambda _: [], retry=lambda _: {})
        self.patches = [patch.object(server, 'manager', self.manager), patch.object(server, 'password_manager', self.passwords)]
        for p in self.patches: p.start()
        self.client = TestClient(server.app)
        self.headers = {'X-Auth-Token': server.auth_token}

    def tearDown(self):
        for p in reversed(self.patches): p.stop()

    def request(self, confirm='LOGOUT_ALL_SESSIONS', headers=None, job_id='live'):
        return self.client.post(f'/api/jobs/{job_id}/logout-sessions', json={'confirm': confirm}, headers=self.headers if headers is None else headers)

    def test_auth_confirmation_and_eligibility(self):
        self.assertEqual(self.request(headers={}).status_code, 401)
        self.assertEqual(self.request(confirm='yes').status_code, 422)
        self.assertEqual(self.request(job_id='missing').status_code, 404)
        self.job.status = 'running'
        self.assertEqual(self.request().status_code, 409)
        self.service.logout_all_sessions.assert_not_awaited()

    def test_password_conflict_and_reverse_lock(self):
        self.passwords.jobs['p'] = SimpleNamespace(email='DEMO@example.com', status='running')
        self.assertEqual(self.request().status_code, 409)
        self.passwords.jobs['p'].status = 'error'
        self.job.sessions_logging_out = True
        self.assertEqual(self.client.post('/api/password/jobs', json={'lines':['demo@example.com|synthetic|TEST']}, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post('/api/password/jobs/p/retry', headers=self.headers).status_code, 409)
        self.service.logout_all_sessions.assert_not_awaited()

    def test_success_and_upstream_error_keep_snapshot(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('password', response.json()['job'])
        self.assertFalse(response.json()['job']['sessions_logging_out'])
        self.service.logout_all_sessions.side_effect = TwoFAFlowError('Chưa xác nhận logout')
        self.assertEqual(self.request().status_code, 502)
        self.assertEqual(self.job.status, 'success')
        self.assertFalse(self.job.sessions_logging_out)
