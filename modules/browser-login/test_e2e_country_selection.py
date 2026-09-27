"""
test_e2e_country_selection.py
-----------------------------
End-to-end integration test using real Playwright browser instance (headless)
to serve realistic OpenAI "Phone number required" mock HTML pages and assert
that select_country_region() actively interacts with the DOM:
  1. Finds the country picker button
  2. Opens the dropdown/popover
  3. Enters search query or scrolls to Cambodia
  4. Clicks 'Cambodia (+855)' option
  5. Updates the trigger display and returns True
"""
import unittest
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from playwright.sync_api import sync_playwright

from auto_login import select_country_region

OPENAI_PHONE_SCREEN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Phone verification - OpenAI</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; padding: 40px; background: #fff; }
    .card { max-width: 440px; margin: 0 auto; }
    h1 { font-size: 24px; font-weight: 600; margin-bottom: 8px; color: #202123; }
    p { font-size: 14px; color: #6e6e80; margin-bottom: 24px; }
    .phone-row { display: flex; align-items: center; border: 1px solid #d9d9e3; border-radius: 6px; padding: 4px 8px; margin-bottom: 20px; }
    .picker-btn { background: none; border: none; font-size: 14px; color: #202123; cursor: pointer; display: flex; align-items: center; gap: 6px; padding: 6px 8px; border-radius: 4px; }
    .picker-btn:hover { background: #f7f7f8; }
    .phone-input { flex: 1; border: none; outline: none; font-size: 14px; padding: 6px 8px; }
    .menu-popover { display: none; position: absolute; background: white; border: 1px solid #d9d9e3; border-radius: 8px; box-shadow: 0 4px 16px rgba(0,0,0,0.1); width: 320px; max-height: 280px; overflow-y: auto; z-index: 100; margin-top: 4px; }
    .menu-popover.open { display: block; }
    .search-box { width: calc(100% - 16px); margin: 8px; padding: 6px 8px; box-sizing: border-box; border: 1px solid #d9d9e3; border-radius: 4px; font-size: 14px; }
    .item-list { list-style: none; padding: 0; margin: 0; }
    .item-list li { padding: 8px 12px; font-size: 14px; cursor: pointer; display: flex; justify-content: space-between; }
    .item-list li:hover { background: #f4f4f6; }
    .submit-btn { width: 100%; padding: 12px; background: #10a37f; color: white; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; }
  </style>
</head>
<body>
  <div class="card">
    <h1>Phone number required</h1>
    <p>To help protect our community, please verify a phone number.</p>

    <div style="position: relative;">
      <div class="phone-row">
        <button id="country-trigger" class="picker-btn" role="combobox" aria-haspopup="listbox" aria-expanded="false">
          <span id="country-flag">🇺🇸</span>
          <span id="country-label">United States (+1)</span>
          <span style="font-size: 10px; color: #8e8ea0;">▼</span>
        </button>
        <input type="tel" name="phone" placeholder="Phone number" class="phone-input" />
      </div>

      <div id="country-popover" class="menu-popover" role="listbox">
        <input type="search" id="country-search" class="search-box" placeholder="Search country" />
        <ul class="item-list" id="country-list">
          <li role="option" data-code="+1" data-country="United States">United States (+1)</li>
          <li role="option" data-code="+84" data-country="Vietnam">Vietnam (+84)</li>
          <li role="option" data-code="+855" data-country="Cambodia">Cambodia (+855)</li>
          <li role="option" data-code="+44" data-country="United Kingdom">United Kingdom (+44)</li>
          <li role="option" data-code="+33" data-country="France">France (+33)</li>
        </ul>
      </div>
    </div>

    <button type="button" class="submit-btn">Send code</button>
  </div>

  <script>
    const trigger = document.getElementById('country-trigger');
    const popover = document.getElementById('country-popover');
    const search = document.getElementById('country-search');
    const list = document.getElementById('country-list');
    const label = document.getElementById('country-label');

    trigger.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = popover.classList.contains('open');
      if (isOpen) {
        popover.classList.remove('open');
        trigger.setAttribute('aria-expanded', 'false');
      } else {
        popover.classList.add('open');
        trigger.setAttribute('aria-expanded', 'true');
        search.focus();
      }
    });

    search.addEventListener('input', () => {
      const q = search.value.toLowerCase().trim();
      const items = list.querySelectorAll('li');
      items.forEach(li => {
        const text = li.innerText.toLowerCase();
        li.style.display = text.includes(q) ? 'flex' : 'none';
      });
    });

    list.addEventListener('click', (e) => {
      const li = e.target.closest('li');
      if (li) {
        label.innerText = li.innerText;
        popover.classList.remove('open');
        trigger.setAttribute('aria-expanded', 'false');
        window._selectedCountry = li.innerText;
      }
    });
  </script>
</body>
</html>
"""

class MockServerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(OPENAI_PHONE_SCREEN_HTML.encode("utf-8"))

    def log_message(self, format, *args):
        pass  # suppress noisy http logs


class RealPlaywrightCountryPickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), MockServerHandler)
        cls.port = cls.server.server_port
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()

    def test_react_aria_virtualized_listbox_data_key_kh(self):
        """Mô phỏng chính xác DOM thật OpenAI: virtualized listbox, data-key=KH."""
        page = self.browser.new_page()
        try:
            page.set_content("""
            <!DOCTYPE html>
            <html>
            <body>
              <h1>Phone number required</h1>
              <p>To help protect our community, please verify a phone number.</p>

              <button id="trigger" aria-haspopup="listbox" aria-expanded="false"
                      data-login-web-auth-control="true"
                      style="font-size:14px; padding:8px 12px; cursor:pointer;">
                United States <bdi>(+1)</bdi>
              </button>

              <!-- React Aria virtualized listbox: full height set, items positioned absolutely -->
              <div id="listbox" role="listbox" tabindex="-1"
                   style="display:none; overflow-y:auto; height:280px; width:327px;
                          border:1px solid #ccc; position:relative;">
                <div style="width:327px; height:9320px; position:relative; pointer-events:auto;">
                  <!-- Only a window of items rendered; Cambodia at top:1360px -->
                  <div style="position:absolute; top:1360px; left:0; width:327px; height:40px;">
                    <div role="option" data-key="KH" aria-selected="false"
                         aria-posinset="35" aria-setsize="233" tabindex="-1"
                         style="display:flex; align-items:center; padding:8px 12px; cursor:pointer;">
                      Cambodia <span style="margin-left:4px; color:#666;">(+855)</span>
                    </div>
                  </div>
                  <div style="position:absolute; top:1400px; left:0; width:327px; height:40px;">
                    <div role="option" data-key="CM" aria-selected="false"
                         aria-posinset="36" aria-setsize="233" tabindex="-1"
                         style="display:flex; align-items:center; padding:8px 12px; cursor:pointer;">
                      Cameroon <span style="margin-left:4px; color:#666;">(+237)</span>
                    </div>
                  </div>
                </div>
              </div>

              <script>
                const trigger = document.getElementById('trigger');
                const listbox = document.getElementById('listbox');
                window._selectedKey = null;

                trigger.addEventListener('click', () => {
                  listbox.style.display = 'block';
                  trigger.setAttribute('aria-expanded', 'true');
                  // Scroll Cambodia into view so its option is in viewport
                  listbox.scrollTop = 1200;
                });

                listbox.addEventListener('click', e => {
                  const opt = e.target.closest('[role="option"]');
                  if (opt) {
                    window._selectedKey = opt.dataset.key;
                    const text = opt.innerText.trim();
                    trigger.innerHTML = text;
                    listbox.style.display = 'none';
                    trigger.setAttribute('aria-expanded', 'false');
                  }
                });
              </script>
            </body>
            </html>
            """)

            success = select_country_region(page, target_country="Cambodia")
            self.assertTrue(success, "Must return True for React Aria virtualized listbox")

            selected_key = page.evaluate("() => window._selectedKey")
            self.assertEqual(selected_key, "KH",
                f"Expected data-key=KH (Cambodia) to be clicked, got: {selected_key}")

        finally:
            page.close()

    def test_real_browser_actively_clicks_and_selects_cambodia(self):
        page = self.browser.new_page()
        try:
            url = f"http://127.0.0.1:{self.port}/"
            page.goto(url)

            # Before action: verify current selection is United States
            label_before = page.locator("#country-label").inner_text()
            self.assertIn("United States (+1)", label_before)

            # Run the actual auto_login function against the real browser DOM!
            success = select_country_region(page, target_country="Cambodia")

            self.assertTrue(success, "select_country_region must return True when Cambodia is found and clicked")

            # After action: verify the DOM was actually updated in the browser!
            label_after = page.locator("#country-label").inner_text()
            self.assertIn("Cambodia (+855)", label_after, "Country trigger must reflect Cambodia (+855) after active click")

            # Verify the picker popover was closed after selection
            popover_visible = page.locator("#country-popover").is_visible()
            self.assertFalse(popover_visible, "Country dropdown popover should be closed after selecting option")

            # Verify JS global recorded the selection
            selected_val = page.evaluate("() => window._selectedCountry")
            self.assertEqual(selected_val, "Cambodia (+855)")

        finally:
            page.close()

    def test_real_browser_scrolls_and_clicks_when_no_search_box(self):
        # Serve a page with 50 countries where Cambodia is buried deep without a search box
        page = self.browser.new_page()
        try:
            page.set_content("""
            <!DOCTYPE html>
            <html>
            <body>
              <div>
                <h1>Verify your phone</h1>
                <p>Add your phone number to continue.</p>
                <button id="trigger" role="combobox" aria-haspopup="listbox" style="margin: 20px;">
                  <span id="label">United States (+1)</span>
                </button>
                <div id="list" role="listbox" style="display:none; height: 100px; overflow-y: scroll; border: 1px solid #ccc;">
                  <div role="option">Albania (+355)</div>
                  <div role="option">Algeria (+213)</div>
                  <div role="option">Andorra (+376)</div>
                  <div role="option">Angola (+244)</div>
                  <div role="option">Argentina (+54)</div>
                  <div role="option">Armenia (+374)</div>
                  <div role="option">Australia (+61)</div>
                  <div role="option">Austria (+43)</div>
                  <div role="option">Azerbaijan (+994)</div>
                  <div role="option">Bahamas (+1242)</div>
                  <div role="option">Bahrain (+973)</div>
                  <div role="option">Bangladesh (+880)</div>
                  <div role="option">Barbados (+1246)</div>
                  <div role="option">Belarus (+375)</div>
                  <div role="option">Belgium (+32)</div>
                  <div role="option">Belize (+501)</div>
                  <div role="option">Benin (+229)</div>
                  <div role="option">Bhutan (+975)</div>
                  <div role="option">Bolivia (+591)</div>
                  <div role="option">Bosnia (+387)</div>
                  <div role="option">Botswana (+267)</div>
                  <div role="option">Brazil (+55)</div>
                  <div role="option">Brunei (+673)</div>
                  <div role="option">Bulgaria (+359)</div>
                  <div role="option">Burkina Faso (+226)</div>
                  <div role="option">Burundi (+257)</div>
                  <div role="option" id="cambodia-opt">Cambodia (+855)</div>
                  <div role="option">Cameroon (+237)</div>
                </div>
              </div>
              <script>
                const trigger = document.getElementById('trigger');
                const list = document.getElementById('list');
                const label = document.getElementById('label');
                trigger.onclick = () => { list.style.display = 'block'; };
                document.getElementById('cambodia-opt').onclick = () => {
                  label.innerText = 'Cambodia (+855)';
                  list.style.display = 'none';
                };
              </script>
            </body>
            </html>
            """)

            success = select_country_region(page, target_country="Cambodia")
            self.assertTrue(success)
            self.assertEqual(page.locator("#label").inner_text(), "Cambodia (+855)")
        finally:
            page.close()
        page = self.browser.new_page()
        try:
            url = f"http://127.0.0.1:{self.port}/"
            page.goto(url)

            # Before action: verify current selection is United States
            label_before = page.locator("#country-label").inner_text()
            self.assertIn("United States (+1)", label_before)

            # Run the actual auto_login function against the real browser DOM!
            success = select_country_region(page, target_country="Cambodia")

            self.assertTrue(success, "select_country_region must return True when Cambodia is found and clicked")

            # After action: verify the DOM was actually updated in the browser!
            label_after = page.locator("#country-label").inner_text()
            self.assertIn("Cambodia (+855)", label_after, "Country trigger must reflect Cambodia (+855) after active click")

            # Verify the picker popover was closed after selection
            popover_visible = page.locator("#country-popover").is_visible()
            self.assertFalse(popover_visible, "Country dropdown popover should be closed after selecting option")

            # Verify JS global recorded the selection
            selected_val = page.evaluate("() => window._selectedCountry")
            self.assertEqual(selected_val, "Cambodia (+855)")

        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()
