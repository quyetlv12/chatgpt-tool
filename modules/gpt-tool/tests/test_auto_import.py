import http.client
import json
from pathlib import Path
import threading
import time

import pytest

from gpt_tool import server


def test_auto_import_checkbox_controls_requests_and_remembers_choice():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    web = Path(__file__).parents[1] / "web"
    requests = []
    errors = []
    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            pytest.skip("Install Playwright Chromium to run the optional UI test")
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))

            def intercept(route):
                path = route.request.url.split("ui.test")[-1]
                if path == "/api/export":
                    requests.append(route.request.post_data_json)
                    route.fulfill(json={"id": "synthetic"})
                elif path == "/api/jobs/synthetic":
                    route.fulfill(json={"done": True, "results": []})
                elif path in ("/", "/app.js", "/styles.css"):
                    file = web / ("index.html" if path == "/" else path[1:])
                    route.fulfill(path=str(file))
                else:
                    route.fulfill(status=404, body="")

            page.route("**/*", intercept)
            page.goto("http://ui.test/")
            toggle = page.locator("#auto-import")
            assert toggle.is_checked(), "preserve existing default"
            page.locator("#lines").fill("synthetic@example.invalid|synthetic|TEST")
            for fmt in ("cockpit", "9router"):
                page.locator(f'input[name="fmt"][value="{fmt}"]').check()
                assert toggle.is_visible()
                for enabled in (False, True):
                    toggle.set_checked(enabled)
                    with page.expect_response("**/api/jobs/synthetic"):
                        page.locator("#run-export").click()
                    page.wait_for_function("document.getElementById('run-export').dataset.action === 'run'")
                    payload = requests[-1]
                    assert payload["cockpit_import"] is (fmt == "cockpit" and enabled)
                    assert payload["nine_router_import"] is (fmt == "9router" and enabled)
            toggle.uncheck()
            page.reload()
            assert not toggle.is_checked(), "off must survive page reload"
            page.locator('input[name="fmt"][value="cockpit"]').check()
            assert not toggle.is_checked(), "changing format must not re-enable import"
            page.locator('input[name="fmt"][value="sub2api"]').check()
            assert not toggle.is_visible()
            page.locator("#lines").fill("synthetic@example.invalid|synthetic|TEST")
            with page.expect_response("**/api/jobs/synthetic"):
                page.locator("#run-export").click()
            assert requests[-1]["cockpit_import"] is False
            assert requests[-1]["nine_router_import"] is False
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("fmt", ["cockpit", "9router"])
def test_disabled_import_still_exports_without_calling_integrations(fmt, monkeypatch):
    calls = []
    completed = threading.Event()

    def export(*args, **kwargs):
        calls.append(("export", kwargs["cockpit_import"], kwargs["nine_router_import"]))
        completed.set()

    monkeypatch.setattr(server, "export_bulk", export)
    monkeypatch.setattr(server, "_send_job_to_cockpit", lambda *args: calls.append("cockpit"))
    monkeypatch.setattr(server, "_send_job_to_nine_router", lambda *args: calls.append("9router"))
    httpd, _ = server._bind("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    job_id = None
    try:
        connection.request("POST", "/api/export", body=json.dumps({
            "format": fmt, "lines": "synthetic@example.invalid|synthetic|TEST",
            "cockpit_import": False, "nine_router_import": False,
        }), headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
        response = connection.getresponse()
        assert response.status == 200
        job_id = json.loads(response.read())["id"]
        assert completed.wait(1)
        deadline = time.monotonic() + 2
        while not server._jobs[job_id]["done"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server._jobs[job_id]["done"]
        assert calls == [("export", False, False)]
        assert server._jobs[job_id]["cockpit"]["enabled"] is False
        assert server._jobs[job_id]["nine_router"]["enabled"] is False
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        if job_id:
            with server._lock:
                server._jobs.pop(job_id, None)
                server._job_cancellations.pop(job_id, None)
