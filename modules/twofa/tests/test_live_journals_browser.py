"""Opt-in real DOM regression, with intercepted localhost fixtures only.

TWOFA_BROWSER_TEST=1 ../../.venv/bin/python -m unittest discover -s tests -p test_live_journals_browser.py
No API server, account workers, real credentials, or installed app is used.
"""
import copy
import json
import os
import unittest
from pathlib import Path
from urllib.parse import urlsplit

STATIC = Path(__file__).resolve().parents[1] / 'change 2fa community' / 'static'


@unittest.skipUnless(os.environ.get('TWOFA_BROWSER_TEST'), 'Explicit local browser fixture opt-in')
class JournalBrowserTests(unittest.TestCase):
    def test_save_clear_reopen_return_and_responsive_controls(self):
        from playwright.sync_api import sync_playwright
        job = dict(id='fixture-live', email='synthetic@example.test', status='success',
            account_state='live', login_verified=True, mode='check_only', plan='plus',
            usage=None, payment_methods=[], created_at=1)
        current, journals, calls = [job], [], []
        settings = {'twofa.max_concurrent': 3, 'twofa.job_timeout': 180,
            'twofa.auto_retry': False, 'twofa.auto_retry_max': 1, 'twofa.auto_retry_delay': 3,
            'twofa.change_enabled': False, 'twofa.read_usage': True, 'twofa.read_payment_methods': True}

        def handle(route):
            request = route.request
            url = urlsplit(request.url)
            if url.hostname != '127.0.0.1':
                route.abort()  # Never contact remote services, including font CDNs.
                return
            path = url.path
            calls.append((request.method, path))
            if path == '/' or path.startswith('/assets/'):
                file = STATIC / ('index.html' if path == '/' else path.removeprefix('/assets/'))
                mime = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml'}.get(file.suffix, 'application/octet-stream')
                route.fulfill(body=file.read_bytes(), content_type=mime)
                return
            result = {}
            if path == '/api/bootstrap':
                result = dict(token='synthetic-local-token', settings=settings, jobs=current)
            elif path == '/api/live-journals':
                if request.method == 'POST':
                    meta = dict(id='journal-one', name=request.post_data_json['name'], account_count=len(current), created_at=1)
                    journals.append(dict(journal=meta, jobs=copy.deepcopy(current)))
                    result = dict(journal=meta)
                else:
                    result = dict(journals=[item['journal'] for item in journals])
            elif path == '/api/live-journals/journal-one':
                result = journals[0]
            elif path == '/api/jobs' and request.method == 'DELETE':
                current.clear()
                result = dict(deleted=1)
            elif path == '/api/password-settings':
                result = dict(configured=False)
            elif path.endswith('/output'):
                route.fulfill(body='', content_type='text/plain')
                return
            else:
                route.fulfill(status=404, json={'detail': 'Unexpected fixture request'})
                return
            route.fulfill(json=result)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/*', handle)
                page.add_init_script('window.EventSource = class { constructor(){ window.fixtureEvents = this; } close(){} };')
                page.goto('http://127.0.0.1:54321/')
                page.wait_for_function("!document.getElementById('save-live-journal').disabled")
                self.assertFalse(page.locator('#live-journal-modal').is_visible())
                self.assertEqual(page.locator('#queue .live-journal-toolbar').count(), 0)
                self.assertTrue(page.locator('#clear-all').is_visible())
                for selector in ('#retry-failed', '#clear-failed', '#logout-selected', '#stop-all'):
                    self.assertFalse(page.locator(selector).is_visible())
                for status in ('queued', 'running'):
                    page.evaluate("status => window.fixtureEvents.onmessage({data: JSON.stringify({type:'snapshot', jobs:[{id:'fixture-active', email:'active@example.test', status, created_at:1}]})})", status)
                    page.wait_for_function("!document.getElementById('stop-all').hidden")
                    self.assertTrue(page.locator('#stop-all').is_visible())
                    self.assertFalse(page.locator('#clear-all').is_visible())
                page.evaluate("jobs => window.fixtureEvents.onmessage({data: JSON.stringify({type:'snapshot', jobs})})", [job])
                page.wait_for_function("document.getElementById('stop-all').hidden")
                self.assertFalse(page.locator('#stop-all').is_visible())
                self.assertTrue(page.locator('#clear-all').is_visible())
                for flag in ('usage_refreshing', 'chat_deleting', 'sessions_logging_out', 'passkey_preparing'):
                    page.evaluate("jobs => window.fixtureEvents.onmessage({data: JSON.stringify({type:'snapshot', jobs})})", [{**job, flag: True}])
                    page.wait_for_function("document.getElementById('clear-all').hidden")
                    self.assertFalse(page.locator('#clear-all').is_visible())
                page.evaluate("jobs => window.fixtureEvents.onmessage({data: JSON.stringify({type:'snapshot', jobs})})", [job])
                page.wait_for_function("!document.getElementById('clear-all').hidden")
                page.locator('#job-list [data-logout-select]').check()
                self.assertTrue(page.locator('#logout-selected').is_visible())
                page.locator('#job-list [data-logout-select]').uncheck()
                self.assertFalse(page.locator('#logout-selected').is_visible())
                page.evaluate("window.fixtureEvents.onmessage({data: JSON.stringify({type:'snapshot', jobs:[{id:'fixture-error', email:'error@example.test', status:'error', account_state:'unknown', created_at:1}]})})")
                page.wait_for_function("!document.getElementById('retry-failed').hidden")
                self.assertTrue(page.locator('#retry-failed').is_visible())
                self.assertTrue(page.locator('#clear-failed').is_visible())
                page.evaluate("jobs => window.fixtureEvents.onmessage({data: JSON.stringify({type:'snapshot', jobs})})", [job])
                page.wait_for_function("document.getElementById('retry-failed').hidden")
                self.assertFalse(page.locator('#clear-failed').is_visible())
                page.locator('#open-live-journal').click()
                self.assertEqual(page.evaluate('document.activeElement.id'), 'live-journal-name')
                for _ in range(8):
                    page.keyboard.press('Tab')
                    # Native dialogs may yield Tab to Chrome's own toolbar,
                    # represented by body; background page controls stay inert.
                    self.assertTrue(page.evaluate("document.activeElement === document.body || document.getElementById('live-journal-modal').contains(document.activeElement)"))
                page.locator('#live-journal-name').focus()
                page.evaluate("document.getElementById('launch-batch').focus()")
                self.assertEqual(page.evaluate('document.activeElement.id'), 'live-journal-name')
                page.keyboard.press('Escape')
                page.wait_for_function("!document.getElementById('live-journal-modal').open")
                self.assertEqual(page.evaluate('document.activeElement.id'), 'open-live-journal')
                page.locator('#open-live-journal').click()
                page.locator('#live-journal-name').fill('Nhật ký thử')
                page.locator('#save-live-journal').click()
                page.wait_for_function("document.querySelectorAll('#live-journal-select option').length === 2")
                self.assertFalse(page.locator('#live-journal-modal').is_visible())
                page.locator('#clear-all').click()
                page.wait_for_function("document.querySelectorAll('#job-list tr').length === 0")
                page.wait_for_function("document.getElementById('clear-all').hidden")
                self.assertFalse(page.locator('#clear-all').is_visible())
                page.locator('#filter-error').click()
                page.locator('#open-live-journal').click()
                page.locator('#live-journal-select').select_option('journal-one')
                page.locator('#job-list tr').wait_for()
                self.assertFalse(page.locator('#live-journal-modal').is_visible())
                self.assertTrue(page.locator('#live-journal-view-status').is_visible())
                self.assertIn('LIVE TẠI THỜI ĐIỂM LƯU', page.locator('#job-list').inner_text())
                self.assertEqual(page.locator('#job-list [data-action]').count(), 1)
                self.assertTrue(page.locator('#clear-all').is_disabled())
                self.assertFalse(page.locator('#clear-all').is_visible())
                self.assertTrue(page.locator('#logout-selected').is_disabled())
                for selector in ('#retry-failed', '#clear-failed', '#logout-selected', '#stop-all'):
                    self.assertFalse(page.locator(selector).is_visible())
                # A real SSE callback clears runtime rows, never historical rows.
                page.evaluate("window.fixtureEvents.onmessage({data: JSON.stringify({type:'snapshot', jobs:[]})})")
                page.wait_for_timeout(150)
                self.assertEqual(page.locator('#job-list tr').count(), 1)
                for width in (1440, 820, 390):
                    page.set_viewport_size({'width': width, 'height': 1000})
                    self.assertTrue(page.locator('#open-live-journal').is_visible())
                    page.locator('#open-live-journal').click()
                    for selector in ('#live-journal-select', '#leave-live-journal', '#live-journal-name'):
                        box = page.locator(selector).bounding_box()
                        self.assertIsNotNone(box)
                        self.assertGreaterEqual(box['x'], 0)
                        self.assertLessEqual(box['x'] + box['width'], width + 1)
                        self.assertGreaterEqual(box['height'], 24)
                    page.locator('#close-live-journal').click()
                    table = page.locator('#queue > .table-wrap').bounding_box()
                    self.assertGreaterEqual(table['height'], 220)
                if os.environ.get('TWOFA_BROWSER_SCREENSHOT'):
                    page.set_viewport_size({'width': 1440, 'height': 1000})
                    page.locator('#open-live-journal').click()
                    page.screenshot(path=os.environ['TWOFA_BROWSER_SCREENSHOT'], full_page=True)
                    page.locator('#close-live-journal').click()
                page.locator('#open-live-journal').click()
                page.locator('#leave-live-journal').click()
                self.assertEqual(page.locator('#job-list tr').count(), 0)
                page.reload()
                page.wait_for_function("document.querySelectorAll('#live-journal-select option').length === 2")
                page.locator('#open-live-journal').click()
                page.locator('#live-journal-select').select_option('journal-one')
                page.locator('#job-list tr').wait_for()
                self.assertEqual(errors, [])
                self.assertFalse(any(path.startswith('/api/jobs/') for _method, path in calls))
            finally:
                browser.close()
