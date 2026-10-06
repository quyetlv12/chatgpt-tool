"""Opt-in headed Chrome regression; synthetic content only, no account login."""
import os
import unittest
from unittest import mock

import auto_login


@unittest.skipUnless(os.environ.get("BROWSER_VIEWPORT_TEST") == "1", "requires headed Chrome")
class NativeBrowserViewportTests(unittest.TestCase):
    def test_one_and_two_windows_keep_bottom_content_visible_after_relayout(self):
        from playwright.sync_api import sync_playwright

        content = (
            '<html><body style="margin:0;height:100vh;overflow:hidden">'
            '<footer style="position:fixed;bottom:0;height:40px">'
            'Synthetic bottom content</footer></body></html>'
        )
        with sync_playwright() as playwright:
            for total in (1, 2):
                with self.subTest(windows=total):
                    browsers = []
                    entries = []
                    try:
                        for index in range(1, total + 1):
                            bounds = auto_login.calculate_window_bounds(index, total)
                            browser = playwright.chromium.launch(channel="chrome", headless=False)
                            browsers.append(browser)
                            context = browser.new_context(**auto_login.build_web_context_options(bounds))
                            context.route("**/*", lambda route: route.abort())
                            page = context.new_page()
                            self.assertTrue(auto_login.apply_browser_window_bounds(context, page, bounds))
                            page.set_content(content)
                            link_page = context.new_page()
                            link_page.set_content(content)
                            entries.append((context, page, link_page, bounds))

                        for context, page, link_page, bounds in entries:
                            for tab in (page, link_page):
                                tab.bring_to_front()
                                self.assert_native_viewport(tab)
                            page.bring_to_front()
                            previous_height = page.evaluate("innerHeight")
                            smaller_bounds = dict(bounds, height=bounds["height"] - 100)
                            with mock.patch.object(auto_login, "emit_web_phase"):
                                self.assertTrue(auto_login.execute_browser_control_command(
                                    {"context": context, "page": page, "link_page": link_page},
                                    {"action": "layout", "bounds": smaller_bounds},
                                ))
                            for tab in (page, link_page):
                                # Chrome applies hidden-tab layout when the user selects it.
                                tab.bring_to_front()
                                tab.wait_for_function(
                                    "innerHeight < {}".format(previous_height), polling=100, timeout=5000,
                                )
                                self.assert_native_viewport(tab)
                    finally:
                        for browser in browsers:
                            browser.close()

    def assert_native_viewport(self, page):
        metrics = page.evaluate(
            '({innerHeight,outerHeight,footerBottom:'
            'document.querySelector("footer").getBoundingClientRect().bottom})'
        )
        self.assertGreaterEqual(metrics["outerHeight"] - metrics["innerHeight"], 50, metrics)
        self.assertIsNone(page.viewport_size)
        self.assertAlmostEqual(metrics["footerBottom"], metrics["innerHeight"], delta=1)


if __name__ == "__main__":
    unittest.main()
