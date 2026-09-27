"""
live_dom_check.py
-----------------
Script kiểm tra DOM thật của màn hình "Phone number required" trên OpenAI.
Chạy headed, mở trang auth, dừng SAU KHI đăng nhập xong (bước 2FA),
rồi dump toàn bộ DOM của vùng phone picker ra file dom_dump.txt để phân tích selector.

Dùng: python3 live_dom_check.py <email> <password> [totp_secret]
"""
import sys
import time
import re
from urllib.parse import urlencode
import secrets as sec_mod
import hashlib
import base64
from playwright.sync_api import sync_playwright

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_URL = "https://auth.openai.com/oauth/authorize"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"


def pkce():
    verifier = base64.urlsafe_b64encode(sec_mod.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def build_url():
    verifier, challenge = pkce()
    state = sec_mod.token_urlsafe(16)
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "codex_cli_rs",
    }
    return AUTH_URL + "?" + urlencode(params)


def fill_visible(page, selectors, value, timeout=8000):
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    loc.fill(value)
                    return True
            except Exception:
                pass
        time.sleep(0.2)
    return False


def click_visible(page, selectors, timeout=2000):
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    loc.click()
                    return True
            except Exception:
                pass
        time.sleep(0.1)
    return False


def dump_dom(page, out="dom_dump.txt"):
    """Dump inner HTML of body to file."""
    try:
        html = page.content()
    except Exception as e:
        html = f"ERROR: {e}"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[dom] Dumped {len(html)} chars to {out}")


def dump_accessibility(page, out="accessibility_dump.txt"):
    """Dump Playwright accessibility tree."""
    try:
        tree = page.accessibility.snapshot()
    except Exception as e:
        tree = f"ERROR: {e}"
    import json
    with open(out, "w", encoding="utf-8") as f:
        json.dump(tree, f, ensure_ascii=False, indent=2)
    print(f"[dom] Accessibility tree dumped to {out}")


def probe_phone_picker(page):
    """
    Probe actual DOM structure of phone country picker.
    Tries all candidate selectors and logs what is visible.
    """
    print("\n====== PROBE: Phone country picker ======")

    # Detect if on phone screen
    try:
        body = page.locator("body").inner_text(timeout=2000).lower()
        phone_screen = any(k in body for k in ("phone number", "add your phone", "verify your phone"))
        print(f"[probe] body contains phone keyword: {phone_screen}")
    except Exception as e:
        print(f"[probe] body read error: {e}")

    # Probe all candidate picker selectors
    picker_candidates = [
        'button[aria-haspopup="listbox"]',
        'button[aria-haspopup="menu"]',
        '[role="combobox"]',
        'button:has-text("(+")',
    ]

    # Also probe generic selector with regex: anything containing (+digit
    for sel in picker_candidates:
        try:
            items = page.locator(sel).all()
            for i, item in enumerate(items):
                try:
                    visible = item.is_visible()
                    text = item.inner_text(timeout=400).strip()[:80]
                    tag = item.evaluate("el => el.tagName")
                    aria_label = item.get_attribute("aria-label") or ""
                    attrs = {
                        "id": item.get_attribute("id"),
                        "class": item.get_attribute("class"),
                        "type": item.get_attribute("type"),
                        "role": item.get_attribute("role"),
                        "aria-haspopup": item.get_attribute("aria-haspopup"),
                        "placeholder": item.get_attribute("placeholder"),
                    }
                    print(f"[probe] sel={sel!r} [{i}]: visible={visible} tag={tag} text={text!r} label={aria_label!r} attrs={attrs}")
                except Exception as e:
                    print(f"[probe] sel={sel!r} [{i}]: error reading: {e}")
        except Exception as e:
            print(f"[probe] sel={sel!r}: error: {e}")

    # Probe inputs
    print("\n[probe] All visible input elements:")
    try:
        for i, inp in enumerate(page.locator("input").all()):
            try:
                if inp.is_visible():
                    attrs = {k: inp.get_attribute(k) for k in ["name","type","placeholder","id","class","autocomplete"]}
                    print(f"  input[{i}]: {attrs}")
            except Exception:
                pass
    except Exception as e:
        print(f"  error: {e}")

    # Probe buttons
    print("\n[probe] All visible button elements:")
    try:
        for i, btn in enumerate(page.locator("button").all()):
            try:
                if btn.is_visible():
                    text = btn.inner_text(timeout=300).strip()[:80]
                    attrs = {k: btn.get_attribute(k) for k in ["id","class","type","aria-haspopup","role"]}
                    print(f"  button[{i}]: text={text!r} attrs={attrs}")
            except Exception:
                pass
    except Exception as e:
        print(f"  error: {e}")

    # Probe listbox/option/menuitem
    for role_sel in ['[role="listbox"]', '[role="option"]', '[role="menu"]', '[role="menuitem"]', 'li']:
        try:
            items = page.locator(role_sel).all()
            visible_items = []
            for item in items:
                try:
                    if item.is_visible():
                        visible_items.append(item.inner_text(timeout=300).strip()[:60])
                except Exception:
                    pass
            if visible_items:
                print(f"\n[probe] {role_sel}: {len(visible_items)} visible items: {visible_items[:10]}")
        except Exception as e:
            print(f"[probe] {role_sel}: error: {e}")

    print("====== END PROBE ======\n")


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 live_dom_check.py <email> <password> [totp_secret]")
        sys.exit(1)

    email = sys.argv[1]
    password = sys.argv[2]
    totp_secret = sys.argv[3] if len(sys.argv) > 3 else None

    url = build_url()
    print(f"[auth] Opening: {url[:80]}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--start-maximized"])
        context = browser.new_context(viewport=None)
        page = context.new_page()

        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        time.sleep(0.5)

        # Step 1: Email
        print("[step1] Filling email...")
        filled = fill_visible(page, [
            'input[name="email"]', 'input[type="email"]',
            'input[name="username"]', 'input[autocomplete="username"]',
        ], email)
        if not filled:
            print("[!] Email field not found. Dumping DOM and stopping.")
            dump_dom(page, "dom_email_missing.txt")
            dump_accessibility(page, "acc_email_missing.txt")
            page.screenshot(path="shot_email_missing.png", full_page=True)
            input("Press ENTER to close...")
            browser.close()
            return

        time.sleep(0.1)
        click_visible(page, [
            'button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Next")',
        ])

        # Step 2: Password
        print("[step2] Filling password...")
        filled = fill_visible(page, [
            'input[name="password"]', 'input[type="password"]',
        ], password, timeout=12000)
        if not filled:
            print("[!] Password field not found. Dumping and stopping.")
            dump_dom(page, "dom_pw_missing.txt")
            dump_accessibility(page, "acc_pw_missing.txt")
            page.screenshot(path="shot_pw_missing.png", full_page=True)
            input("Press ENTER to close...")
            browser.close()
            return

        time.sleep(0.1)
        click_visible(page, [
            'button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Log in")',
        ])
        time.sleep(1)

        # Step 3: TOTP if present
        if totp_secret:
            try:
                import pyotp
                code = pyotp.TOTP(totp_secret).now()
                print(f"[step3] TOTP code: {code}")
                otp_input = None
                deadline = time.time() + 10
                while time.time() < deadline:
                    for sel in ['input[name="code"]', 'input[inputmode="numeric"]', 'input[autocomplete="one-time-code"]']:
                        try:
                            loc = page.locator(sel).first
                            if loc.is_visible():
                                otp_input = loc
                                break
                        except Exception:
                            pass
                    if otp_input:
                        break
                    time.sleep(0.2)

                if otp_input:
                    otp_input.fill(code)
                    time.sleep(0.1)
                    click_visible(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Verify")'])
                    time.sleep(2)
                else:
                    print("[2FA] OTP field not found (may be bypassed).")
            except Exception as e:
                print(f"[2FA] Error: {e}")

        # Step 3.5: Check for phone/country screen
        print("[step3.5] Waiting up to 10s to detect phone screen...")
        phone_detected = False
        for _ in range(40):
            try:
                body = page.locator("body").inner_text(timeout=500).lower()
                if any(k in body for k in ("phone number required", "add your phone number", "verify your phone", "country")):
                    phone_detected = True
                    break
            except Exception:
                pass
            time.sleep(0.25)

        # Always take screenshot + dump DOM at current state
        page.screenshot(path="shot_country_screen.png", full_page=True)
        print("[snapshot] Saved shot_country_screen.png")
        dump_dom(page, "dom_country_screen.txt")
        dump_accessibility(page, "acc_country_screen.txt")

        if phone_detected:
            print("[found] Phone/country screen DETECTED. Running probe...")
            probe_phone_picker(page)

            # Test: try to click country picker
            print("\n[test] Attempting to click country picker button...")
            picker_candidates = [
                'button[aria-haspopup="listbox"]',
                'button[aria-haspopup="menu"]',
                '[role="combobox"]',
                'button:has-text("(+")',
            ]
            clicked_picker = False
            for sel in picker_candidates:
                try:
                    items = page.locator(sel).all()
                    for candidate in items:
                        if not candidate.is_visible():
                            continue
                        text = candidate.inner_text(timeout=400).strip()
                        import re as _re
                        if _re.search(r"\(\+\d{1,4}\)", text):
                            print(f"[test] Clicking picker: {text!r} (sel={sel!r})")
                            candidate.click()
                            clicked_picker = True
                            time.sleep(0.8)
                            # Dump list DOM
                            page.screenshot(path="shot_picker_open.png")
                            print("[snapshot] Saved shot_picker_open.png")
                            probe_phone_picker(page)  # probe again after opening
                            break
                except Exception as e:
                    print(f"[test] sel={sel!r}: {e}")
                if clicked_picker:
                    break

            if not clicked_picker:
                print("[!] No picker button found via selector. Dumping extra accessibility tree.")

            # Try to find Cambodia
            print("\n[test] Searching for 'Cambodia' in open list...")
            cambodia_sels = [
                'text="Cambodia (+855)"',
                '[role="option"]:has-text("Cambodia (+855)")',
                '[role="menuitem"]:has-text("Cambodia (+855)")',
                'li:has-text("Cambodia (+855)")',
                'button:has-text("Cambodia (+855)")',
                'text="Cambodia"',
                '[role="option"]:has-text("Cambodia")',
                'li:has-text("Cambodia")',
            ]
            found_cambodia = False
            for sel in cambodia_sels:
                try:
                    opt = page.locator(sel).first
                    if opt.is_visible():
                        print(f"[test] FOUND Cambodia option via {sel!r}! Clicking...")
                        opt.click()
                        found_cambodia = True
                        time.sleep(0.5)
                        page.screenshot(path="shot_cambodia_selected.png")
                        print("[snapshot] Saved shot_cambodia_selected.png")
                        break
                except Exception as e:
                    print(f"[test] {sel!r}: {e}")

            if not found_cambodia:
                print("[!] Cambodia not found in visible list. List may need scrolling.")
                # Try scroll + text match
                try:
                    for candidate in page.locator("text=Cambodia").all():
                        if candidate.is_visible():
                            candidate.scroll_into_view_if_needed()
                            time.sleep(0.3)
                            candidate.click()
                            found_cambodia = True
                            print("[test] Scrolled and clicked Cambodia")
                            time.sleep(0.5)
                            page.screenshot(path="shot_cambodia_scroll.png")
                            break
                except Exception as e:
                    print(f"[test] scroll cambodia: {e}")

            if found_cambodia:
                print("\n[SUCCESS] Cambodia selected successfully in live browser!")
            else:
                print("\n[FAIL] Could not find/click Cambodia option.")
        else:
            print("[step3.5] Phone screen NOT detected at this time.")
            print("Current URL:", page.url)
            try:
                body = page.locator("body").inner_text(timeout=500)[:400]
                print("Body text:", body)
            except Exception:
                pass

        print("\nScript done. Browser kept open. Check screenshot files.")
        print("Files: shot_country_screen.png, dom_country_screen.txt, acc_country_screen.txt")
        input("Press ENTER to close browser and exit...")
        browser.close()


if __name__ == "__main__":
    main()
