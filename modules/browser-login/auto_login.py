"""
Auto-login ChatGPT accounts via OAuth PKCE flow.
Uses Playwright to automate browser login, fills email/password/2FA,
catches the OAuth callback, and imports refresh_token into 9router.

Input format (one per line):
  email|password|2fa_secret

Usage:
  python auto_login.py accounts.txt           # headless
  python auto_login.py accounts.txt --headed   # show browser
  python auto_login.py accounts.txt --slow     # slow mode for debugging
"""

import sys
import os
import json
import time
import base64
import hashlib
import math
import re
import secrets
import subprocess
import threading
import queue
import heapq
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# Force UTF-8 + unbuffered output on Windows so server.py receives log
# lines in real-time instead of waiting for Python's pipe buffer.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
else:
    # On non-Windows, ensure line-buffered output
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

# ---------- TOTP ----------
try:
    import pyotp
except ImportError:
    print("[!] pyotp not installed. Run: python -m pip install pyotp")
    sys.exit(1)

# ---------- Playwright ----------
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[!] playwright not installed. Run: python -m pip install playwright && python -m playwright install chromium")
    sys.exit(1)

# ---------- OAuth Config (same as 9router) ----------
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
SCOPE = "openid profile email offline_access"
CALLBACK_PORT = 1455
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/auth/callback"
IMPORT_API = os.environ.get("SHOPTAIKHOAN_IMPORT_API", "http://localhost:9876/api/import")
CHATGPT_URL = "https://chatgpt.com/"
CHATGPT_LOGIN_URL = "https://chatgpt.com/auth/login"
DEFAULT_WEB_LINK = ""  # Only an explicit --open-link enables the secondary tab.
DESKTOP_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
MIN_DESKTOP_WINDOW_WIDTH = 900
MIN_BATCH_SCREEN_WIDTH = 1600
SINGLE_WINDOW_VERTICAL_PADDING = 32
_event_output_lock = threading.Lock()
_screen_size_lock = threading.Lock()
_screen_size_cache = None


def detect_primary_screen_size():
    """Return primary screen dimensions in desktop points with safe fallbacks."""
    global _screen_size_cache
    with _screen_size_lock:
        if _screen_size_cache:
            return _screen_size_cache

        try:
            env_width = int(os.environ.get("SHOPTAIKHOAN_SCREEN_WIDTH", "0"))
            env_height = int(os.environ.get("SHOPTAIKHOAN_SCREEN_HEIGHT", "0"))
            if env_width > 0 and env_height > 0:
                _screen_size_cache = (env_width, env_height)
                return _screen_size_cache
        except (TypeError, ValueError):
            pass

        try:
            if sys.platform == "darwin":
                # Finder reports the union of every connected display. On a
                # desktop with a monitor left of the primary display that can
                # look like ``-1512, 0, 2048, 1152``; subtracting those bounds
                # incorrectly creates a 3560px-wide "screen". AppKit exposes
                # the actual main display instead.
                output = subprocess.check_output(
                    [
                        "/usr/bin/osascript",
                        "-l",
                        "JavaScript",
                        "-e",
                        'ObjC.import("AppKit"); var f=$.NSScreen.mainScreen.frame; '
                        '[Number(f.origin.x),Number(f.origin.y),'
                        'Number(f.size.width),Number(f.size.height)].join(",")',
                    ],
                    text=True,
                    timeout=3,
                    stderr=subprocess.DEVNULL,
                )
                values = [int(float(value)) for value in re.findall(r"-?\d+(?:\.\d+)?", output)]
                if len(values) >= 4:
                    width, height = values[2], values[3]
                    if width > 0 and height > 0:
                        _screen_size_cache = (width, height)
                        return _screen_size_cache

                output = subprocess.check_output(
                    [
                        "/usr/bin/osascript",
                        "-e",
                        'tell application "Finder" to get bounds of window of desktop',
                    ],
                    text=True,
                    timeout=3,
                    stderr=subprocess.DEVNULL,
                )
                values = [int(value) for value in re.findall(r"-?\d+", output)]
                if len(values) >= 4:
                    left, top, right, bottom = values[:4]
                    # Accept Finder only for a single-display desktop. Its
                    # multi-display result is a union, not the main display.
                    if left == 0 and top == 0 and right > 0 and bottom > 0:
                        _screen_size_cache = (right - left, bottom - top)
                        return _screen_size_cache
            elif sys.platform == "win32":
                import ctypes
                width = int(ctypes.windll.user32.GetSystemMetrics(0))
                height = int(ctypes.windll.user32.GetSystemMetrics(1))
                if width > 0 and height > 0:
                    _screen_size_cache = (width, height)
                    return _screen_size_cache
        except Exception:
            pass

        _screen_size_cache = (1920, 1080)
        return _screen_size_cache


def calculate_window_bounds(index, total, screen_width=None, screen_height=None):
    """Assign a stable, non-overlapping grid cell to one 1-based account index."""
    if total < 1:
        raise ValueError("total must be a positive integer")
    if index < 1 or index > total:
        raise ValueError("index must be between 1 and total")
    if screen_width is None or screen_height is None:
        screen_width, screen_height = detect_primary_screen_size()

    screen_width = max(320, int(screen_width))
    screen_height = max(240, int(screen_height))
    gap = 10
    top_inset = 34 if sys.platform == "darwin" else 0
    bottom_inset = 74 if sys.platform == "darwin" else 40
    usable_height = max(1, screen_height - top_inset - bottom_inset)
    screen_aspect = screen_width / usable_height
    ideal_columns = max(1, int(round(math.sqrt(total * screen_aspect))))
    desktop_columns = max(1, (screen_width - gap) // (MIN_DESKTOP_WINDOW_WIDTH + gap))
    columns = min(total, ideal_columns, desktop_columns)
    if total >= 4 and screen_width >= MIN_BATCH_SCREEN_WIDTH:
        columns = max(columns, min(total, 2))
    rows = int(math.ceil(total / columns))
    width = max(1, (screen_width - gap * (columns + 1)) // columns)
    height = max(1, (usable_height - gap * (rows + 1)) // rows)
    slot = index - 1
    column = slot % columns
    row = slot // columns
    single_window_padding = SINGLE_WINDOW_VERTICAL_PADDING if total == 1 else 0
    return {
        "left": gap + column * (width + gap),
        "top": top_inset + gap + row * (height + gap) + single_window_padding,
        "width": width,
        "height": max(1, height - single_window_padding * 2),
    }


def build_web_context_options(window_bounds):
    """Let Chrome's content area follow the native tile, excluding browser UI."""
    return {
        "no_viewport": True,
        "is_mobile": False,
        "has_touch": False,
        "user_agent": DESKTOP_USER_AGENT,
    }


def apply_browser_window_bounds(context, page, bounds):
    """Apply exact native Chrome window bounds through the DevTools protocol."""
    session = None
    try:
        session = context.new_cdp_session(page)
        window = session.send("Browser.getWindowForTarget")
        session.send(
            "Browser.setWindowBounds",
            {
                "windowId": window["windowId"],
                "bounds": {
                    "left": int(bounds["left"]),
                    "top": int(bounds["top"]),
                    "width": int(bounds["width"]),
                    "height": int(bounds["height"]),
                    "windowState": "normal",
                },
            },
        )
        return True
    except Exception:
        return False
    finally:
        if session:
            try:
                session.detach()
            except Exception:
                pass


def queue_browser_control_command(action, index=None, screen_width=None, screen_height=None):
    """Queue UI control work for execution on each browser's owning thread."""
    if action not in ("focus", "rearrange"):
        raise ValueError("Unsupported browser control action")
    if action == "focus":
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise ValueError("Browser index must be a positive integer")
        if index < 1:
            raise ValueError("Browser index must be a positive integer")

    with _kept_sessions_lock:
        sessions = sorted(
            _kept_browser_sessions.values(),
            key=lambda entry: int(entry.get("index") or 0),
        )

    if action == "focus":
        sessions = [entry for entry in sessions if int(entry.get("index") or 0) == index]
        for entry in sessions:
            entry["commands"].put({"action": "focus"})
        return len(sessions)

    if not sessions:
        return 0
    if screen_width is None or screen_height is None:
        screen_width, screen_height = detect_primary_screen_size()
    for position, entry in enumerate(sessions, start=1):
        try:
            layout_total = int(entry.get("layout_total") or len(sessions))
            layout_index = int(entry.get("layout_index") or position)
        except (TypeError, ValueError):
            layout_total = len(sessions)
            layout_index = position
        if layout_total < 1 or layout_index < 1 or layout_index > layout_total:
            layout_total = len(sessions)
            layout_index = position
        entry["commands"].put({
            "action": "layout",
            "bounds": calculate_window_bounds(
                layout_index,
                layout_total,
                screen_width,
                screen_height,
            ),
        })
    return len(sessions)


def execute_browser_control_command(entry, command):
    """Execute one browser command on the Playwright thread that owns it."""
    action = command.get("action") if isinstance(command, dict) else ""
    page = entry.get("page")
    email = entry.get("email", "")
    index = int(entry.get("index") or 0)
    try:
        if action == "focus":
            page.bring_to_front()
            emit_web_phase(email, index, "window_focus", "Đã đưa cửa sổ ChatGPT lên trước")
            return True
        if action == "layout":
            bounds = command.get("bounds") or {}
            applied = apply_browser_window_bounds(entry.get("context"), page, bounds)
            if applied:
                emit_web_phase(
                    email,
                    index,
                    "window_relayout",
                    "Đã sắp xếp lại cửa sổ ({}×{})".format(bounds["width"], bounds["height"]),
                )
            return applied
    except Exception as error:
        emit_web_phase(email, index, "window_control_error", "Không thể điều khiển cửa sổ: {}".format(error))
    return False


def retained_browser_session_is_alive(entry):
    """Return whether a retained Playwright session still has a live browser.

    Playwright objects can raise when their browser process has been closed, so
    this check deliberately treats those exceptions as a dead session.  The
    explicit ``is True``/``is False`` checks also keep test doubles and older
    Playwright implementations that do not expose these methods compatible.
    """
    browser = entry.get("browser")
    if browser is not None:
        is_connected = getattr(browser, "is_connected", None)
        if callable(is_connected):
            try:
                if is_connected() is False:
                    return False
            except Exception:
                return False

    page = entry.get("page")
    if page is not None:
        is_closed = getattr(page, "is_closed", None)
        if callable(is_closed):
            try:
                if is_closed() is True:
                    return False
            except Exception:
                return False

    # A browser context can disappear without the page method becoming
    # observable first.  Only inspect concrete page lists; mocks and custom
    # context wrappers should not be interpreted as an empty list.
    context = entry.get("context")
    try:
        pages = getattr(context, "pages", None) if context is not None else None
        if isinstance(pages, (list, tuple)) and page is not None and page not in pages:
            return False
    except Exception:
        return False
    return True


def service_retained_browser_commands(session_key, entry):
    """Keep a retained Playwright browser responsive to UI commands."""
    commands = entry["commands"]
    while not _session_shutdown_event.is_set():
        if not retained_browser_session_is_alive(entry):
            break
        try:
            command = commands.get(timeout=0.25)
        except queue.Empty:
            continue
        execute_browser_control_command(entry, command)
    with _kept_sessions_lock:
        if _kept_browser_sessions.get(session_key) is entry:
            _kept_browser_sessions.pop(session_key, None)
        if not _kept_browser_sessions:
            _session_shutdown_event.set()


def listen_for_browser_control_commands(stream=None):
    """Read newline-delimited JSON commands sent by the local manager."""
    source = stream if stream is not None else sys.stdin
    if source is None:
        return
    for raw_line in source:
        if _session_shutdown_event.is_set():
            break
        try:
            command = json.loads(raw_line)
            action = command.get("action")
            queued = queue_browser_control_command(action, index=command.get("index"))
            print("CONTROL_QUEUED|{}|{}".format(action, queued), flush=True)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            print("CONTROL_ERROR|{}".format(str(error).replace("|", "/")), flush=True)

# ---------- PKCE ----------
def generate_pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def build_auth_url():
    """Build a fresh OAuth URL using the registered Codex callback URI."""
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)
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
    return AUTH_URL + "?" + urlencode(params), verifier, state


def build_codex_auth_url():
    """Build a fresh Codex OAuth URL; this tool owns the PKCE transaction."""
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid profile email offline_access api.connectors.read api.connectors.invoke",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "codex_streamlined_login": "true",
        "state": state,
        "originator": "Codex Desktop",
    }
    return AUTH_URL + "?" + urlencode(params), verifier, state


def exchange_code(code, verifier):
    body = json.dumps({
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }).encode()
    req = Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        resp = urlopen(req, timeout=30)
        return json.loads(resp.read().decode()), None
    except HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
    except Exception as e:
        return None, str(e)


def decode_jwt_email(access_token):
    try:
        parts = access_token.split(".")
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        prof = decoded.get("https://api.openai.com/profile", {})
        auth = decoded.get("https://api.openai.com/auth", {})
        return {
            "email": prof.get("email", ""),
            "account_id": auth.get("chatgpt_account_id", ""),
            "plan_type": auth.get("chatgpt_plan_type", ""),
        }
    except:
        return {"email": "", "account_id": "", "plan_type": ""}


def tokens_to_connection(tokens):
    at = tokens.get("access_token", "")
    info = decode_jwt_email(at)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    exp_in = tokens.get("expires_in", 864000)
    exp_at = datetime.fromtimestamp(
        time.time() + exp_in, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    return {
        "accessToken": at,
        "refreshToken": tokens.get("refresh_token", ""),
        "idToken": tokens.get("id_token", ""),
        "expiresAt": exp_at,
        "expiresIn": exp_in,
        "testStatus": "active",
        "lastUsedAt": now,
        "consecutiveUseCount": 0,
        "backoffLevel": 0,
        "providerSpecificData": {
            "chatgptAccountId": info["account_id"],
            "chatgptPlanType": info["plan_type"],
        },
        "lastError": None,
        "lastErrorAt": None,
        "email": info["email"],
        "name": info["email"],
        "provider": "codex",
        "authType": "oauth",
    }


def import_to_9router(conn):
    """Import via the local server and require SQLite verification for this email."""
    body = json.dumps({"connections": [conn]}).encode("utf-8")
    req = Request(IMPORT_API, data=body, headers={
        "Content-Type": "application/json; charset=utf-8",
    })
    try:
        resp = urlopen(req, timeout=15)
        payload = json.loads(resp.read().decode("utf-8"))
        if not payload.get("sqliteVerified"):
            detail = "; ".join(payload.get("errors") or []) or "email not verified in 9router SQLite"
            return None, detail
        return payload, None
    except Exception as e:
        return None, "{}: {}".format(type(e).__name__, str(e))


# ---------- Shared callback dispatcher ----------
class CallbackResult:
    def __init__(self):
        self.code = None
        self.state = None
        self.error = None
        self.done = threading.Event()


_callback_results = {}
_callback_lock = threading.RLock()
_callback_server = None
_callback_thread = None

# Keep selected headed browser sessions alive after OAuth completes so the user
# can continue working in the normal ChatGPT web app. The parent server closes
# these processes through its existing auto-stop process-tree cleanup.
_kept_browser_sessions = {}
_kept_sessions_lock = threading.RLock()
_session_shutdown_event = threading.Event()


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/auth/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = parse_qs(parsed.query)
        state = params.get("state", [None])[0]
        with _callback_lock:
            result = _callback_results.get(state)
        if not result:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<html><body>OAuth callback khong hop le hoac da het han.</body></html>".encode("utf-8"))
            return
        result.code = params.get("code", [None])[0]
        result.state = state
        result.error = params.get("error", [None])[0]
        result.done.set()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<html><body style='font-family:system-ui;background:#1a1a2e;color:#e0e0e0;display:flex;justify-content:center;align-items:center;height:100vh;margin:0'><div style='text-align:center'><h2>✅ Thành công!</h2><p>Vui lòng không tắt, để nó tự động hoàn tất.</p></div></body></html>".encode("utf-8"))


def start_callback_dispatcher():
    """Start exactly one server on OpenAI's registered callback port."""
    global _callback_server, _callback_thread
    with _callback_lock:
        if _callback_server:
            return
        class ReusableHTTPServer(HTTPServer):
            allow_reuse_address = True
        _callback_server = ReusableHTTPServer(("127.0.0.1", CALLBACK_PORT), CallbackHandler)
        _callback_thread = threading.Thread(target=_callback_server.serve_forever, daemon=True)
        _callback_thread.start()


def stop_callback_dispatcher():
    global _callback_server, _callback_thread
    with _callback_lock:
        server = _callback_server
        _callback_server = None
        _callback_thread = None
        _callback_results.clear()
    if server:
        server.shutdown()
        server.server_close()


def register_callback(state):
    result = CallbackResult()
    with _callback_lock:
        _callback_results[state] = result
    return result


def unregister_callback(state):
    with _callback_lock:
        _callback_results.pop(state, None)


# ---------- Browser automation ----------
def debug_page(page, label):
    """Save screenshot + log page URL/title for debugging login flow."""
    if os.environ.get("SHOPTAIKHOAN_DEBUG_SCREENSHOTS") != "1":
        return
    try:
        safe = ''.join(c if c.isalnum() or c in ('_', '-') else '_' for c in label)[:60]
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"debug_{safe}.png")
        page.screenshot(path=path, full_page=True)
        print(f"    [debug] {label}: title={page.title()!r} url={page.url}")
        print(f"    [debug] screenshot: {path}")
    except Exception as e:
        print(f"    [debug] failed: {e}")


def click_first_visible(page, selectors, timeout=3000):
    """Click the first visible selector from a list. Races all selectors."""
    import time as _time
    # Quick check: any already visible?
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible():
                loc.click()
                return True
        except Exception:
            pass
    # Poll until timeout
    deadline = _time.time() + timeout / 1000
    while _time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    loc.click()
                    return True
            except Exception:
                pass
        _time.sleep(0.1)
    return False


def fill_first_visible(page, selectors, value, timeout=12000):
    """Fill the first visible input from a list. Races all selectors at once."""
    # Strategy 1: Try OR-combined selector for instant match
    combined = " >> visible=true, ".join(selectors)
    try:
        # Build a single locator that matches ANY of the selectors
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    loc.fill(value)
                    return loc
            except Exception:
                pass
    except Exception:
        pass

    # Strategy 2: Poll all selectors rapidly until timeout
    import time as _time
    deadline = _time.time() + timeout / 1000
    while _time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    loc.fill(value)
                    return loc
            except Exception:
                pass
        _time.sleep(0.15)  # Small poll interval

    # Strategy 3: One last sequential attempt with short timeout each
    per_sel = max(500, timeout // len(selectors)) if selectors else timeout
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=per_sel)
            loc.fill(value)
            return loc
        except Exception:
            pass
    return None


def wait_a_bit(page, ms=1500):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=ms)
    except Exception:
        pass
    time.sleep(ms / 1000)


def normalize_totp_secret(secret):
    """Normalize base32 TOTP secret, extract from otpauth URL if needed, and clean formatting."""
    s = (secret or "").strip()
    if s.startswith("otpauth://"):
        try:
            parsed = urlparse(s)
            qs = parse_qs(parsed.query)
            s = qs.get("secret", [s])[0]
        except Exception:
            pass
    # Remove whitespace, hyphens, and existing padding
    s = s.replace(" ", "").replace("-", "").replace("=", "").upper()
    return s


def get_2fa_live_code(clean_secret):
    """Fetch 2FA TOTP code from 2fa.live API."""
    if not clean_secret:
        return None
    url = f"https://2fa.live/tok/{clean_secret}"
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    })
    try:
        with urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            token = str(data.get("token", "")).strip()
            if token and token.isdigit():
                return token
    except Exception as e:
        print(f"    [!] 2fa.live request failed: {e}")
    return None


def get_2fa_code(secret, attempt=1):
    """
    Get 2FA code according to strategy:
    - Attempt 1: 2fa.live
    - Attempt 2: 2fa.live
    - Attempt 3+: current local logic (pyotp)
    """
    clean_secret = normalize_totp_secret(secret)
    if not clean_secret:
        return ""

    # Time Window Guard: if under 4 seconds remain in current 30s window,
    # wait for fresh window so code won't expire while submitting.
    time_remaining = 30 - (int(time.time()) % 30)
    if time_remaining <= 4:
        print(f"    [2FA] Window expiring in {time_remaining}s, waiting for fresh 30s cycle...")
        time.sleep(time_remaining + 0.5)

    # Attempt 1 & 2: 2fa.live
    if attempt in (1, 2):
        print(f"    [2FA] (Attempt {attempt}/3) Fetching code from 2fa.live...")
        code = get_2fa_live_code(clean_secret)
        if code:
            print("    [2FA] Successfully retrieved from 2fa.live")
            return code
        print("    [!] 2fa.live returned empty, retrying 2fa.live once...")
        time.sleep(1)
        code = get_2fa_live_code(clean_secret)
        if code:
            print("    [2FA] Successfully retrieved from 2fa.live (retry)")
            return code
        print("    [!] 2fa.live unavailable, falling back to local pyotp...")

    # Attempt 3+ (or fallback): pyotp
    print(f"    [2FA] (Attempt {attempt}/3) Using local pyotp logic...")
    padded = clean_secret
    pad = len(padded) % 8
    if pad in (2, 4, 5, 7):
        padded += "=" * (8 - pad)
    return pyotp.TOTP(padded).now()


def make_totp_code(secret):
    """Generate current TOTP code safely (backwards compatibility)."""
    return get_2fa_code(secret, attempt=1)


def login_account(page, email, password, totp_secret, headed=False, attempt=1):
    """Navigate one account's OAuth flow through the registered shared callback."""
    auth_url, verifier, state = build_auth_url()
    result = register_callback(state)

    try:
        # Navigate to auth URL
        page.goto(auth_url, wait_until="domcontentloaded", timeout=45000)
        wait_a_bit(page, 300)
        debug_page(page, "01_open_auth")

        # Do NOT click generic Continue/Login here. OpenAI's first screen usually
        # contains the email form directly. Since we launch a fresh browser per
        # account there is no previous session, so skip the chooser entirely.

        # --- Step 1: Email ---
        email_input = fill_first_visible(page, [
            'input[name="email"]',
            'input[type="email"]',
            'input[name="username"]',
            'input[id*="email" i]',
            'input[placeholder*="email" i]',
            'input[autocomplete="username"]',
            'input:not([type="hidden"]):not([type="password"])',
        ], email, timeout=10000)

        if not email_input:
            debug_page(page, "02_email_not_found")
            return None, "Email input not found", verifier, state

        time.sleep(0.05)
        if not click_first_visible(page, [
            'button[type="submit"]',
            'button:has-text("Continue")',
            'button:has-text("Next")',
            'button:has-text("Tiếp tục")',
            'button:has-text("Log in")',
        ], timeout=900):
            email_input.press("Enter")
        # Do not sleep here. Wait directly for the password selector below so the
        # password is filled immediately when the field appears.
        debug_page(page, "03_after_email")

        # Some accounts are redirected to Apple/iCloud auth or another IdP.
        # Handle generic email/password pages too.
        # --- Step 2: Password ---
        pwd_input = fill_first_visible(page, [
            'input[name="password"]',
            'input[type="password"]',
            'input[id*="password" i]',
            'input[autocomplete="current-password"]',
            'input[placeholder*="password" i]',
            'input[placeholder*="mật khẩu" i]',
        ], password, timeout=9000)

        if not pwd_input:
            debug_page(page, "04_password_not_found")
            return None, "Password input not found", verifier, state

        time.sleep(0.05)
        if not click_first_visible(page, [
            'button[type="submit"]',
            'button:has-text("Continue")',
            'button:has-text("Next")',
            'button:has-text("Log in")',
            'button:has-text("Sign in")',
            'button:has-text("Đăng nhập")',
        ], timeout=900):
            pwd_input.press("Enter")
        wait_a_bit(page, 600)
        debug_page(page, "05_after_password")

        # --- Step 3: 2FA (TOTP) ---
        if totp_secret:
            otp_selectors = [
                'input[name="code"]',
                'input[inputmode="numeric"]',
                'input[autocomplete="one-time-code"]',
                'input[id*="code" i]',
                'input[placeholder*="code" i]',
                'input[placeholder*="verification" i]',
                'input[aria-label*="code" i]',
            ]

            # Poll for OTP input, or detect early if already redirected to consent / callback / workspace select
            otp_input = None
            otp_deadline = time.time() + 10
            while time.time() < otp_deadline:
                # If already passed to callback or error screen, break early
                if "localhost" in page.url and "/auth/callback" in page.url:
                    break
                try:
                    for sel in otp_selectors:
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
                try:
                    otp_code = get_2fa_code(totp_secret, attempt=attempt)
                except Exception as e:
                    debug_page(page, "06_totp_secret_error")
                    return None, f"Invalid 2FA secret: {e}", verifier, state

                if not otp_code:
                    return None, "Failed to obtain 2FA code", verifier, state

                otp_input.fill(otp_code)
                print("    [2FA] Filled TOTP code")
                time.sleep(0.15)
                if not click_first_visible(page, [
                    'button[type="submit"]',
                    'button:has-text("Continue")',
                    'button:has-text("Verify")',
                    'button:has-text("Next")',
                    'button:has-text("Submit")',
                ], timeout=2500):
                    otp_input.press("Enter")
                wait_a_bit(page, 2500)
                debug_page(page, "06_after_2fa")

                # Check if OpenAI rejected the code
                try:
                    body_text = page.locator("body").inner_text(timeout=500).lower()
                    if any(msg in body_text for msg in ["code is invalid", "that code wasn't valid", "invalid code", "mã không hợp lệ", "incorrect code"]):
                        print("    [2FA] ⚠️ OpenAI rejected the 2FA code")
                        debug_page(page, "06_2fa_rejected")
                        return None, "RETRYABLE_2FA: OpenAI rejected 2FA code (invalid code)", verifier, state
                except Exception:
                    pass
            else:
                print("    [2FA] No TOTP prompt found (or bypassed)")
                debug_page(page, "06_2fa_not_found")

        # --- Step 3.5: Country/region selection (appears after 2FA on some accounts) ---
        try:
            country_selected = select_country_region(page)
            if country_selected:
                print("    [country] Cambodia selected", flush=True)
                wait_a_bit(page, 800)
        except Exception as _country_err:
            print(f"    [!] Country step exception (non-fatal): {_country_err}")

        # --- Step 4: Consent + callback ---
        # If final consent/authorization keeps loading, retry the consent click up
        # to 2 more times before marking the account failed.
        got_callback = False
        for consent_try in range(3):
            # Check if already on callback before trying consent clicks
            if "localhost" in page.url and "/auth/callback" in page.url:
                got_callback = result.done.wait(timeout=1)
                if not got_callback:
                    parsed = urlparse(page.url)
                    params = parse_qs(parsed.query)
                    callback_state = params.get("state", [None])[0]
                    if callback_state == state:
                        result.code = params.get("code", [None])[0]
                        result.state = callback_state
                        result.error = params.get("error", [None])[0]
                        got_callback = result.code is not None or result.error is not None
                if got_callback:
                    break

            # First, check if this is a phone verification screen. If so, run
            # country selection and then wait for the user to finish; do NOT
            # press any Continue/Submit button and do NOT reload.
            try:
                body_text_check = page.locator("body").inner_text(timeout=500).lower()
            except Exception:
                body_text_check = ""

            is_phone_screen = any(p in body_text_check for p in (
                "phone number required", "add your phone number",
                "verify your phone", "phone verification",
            ))

            if is_phone_screen:
                print("    [phone] Phone verification screen detected.", flush=True)
                try:
                    select_country_region(page)
                except Exception:
                    pass
                print("    [phone] Waiting for user to complete phone verification...", flush=True)
                # Poll up to 120s for user to finish phone step
                for _pw in range(120):
                    if "localhost" in page.url and "/auth/callback" in page.url:
                        break
                    time.sleep(1)
                # Re-check callback
                try:
                    current = page.url
                    if "localhost" in current and "/auth/callback" in current:
                        parsed = urlparse(current)
                        params = parse_qs(parsed.query)
                        callback_state = params.get("state", [None])[0]
                        if callback_state == state:
                            result.code = params.get("code", [None])[0]
                            result.state = callback_state
                            result.error = params.get("error", [None])[0]
                            got_callback = result.code is not None or result.error is not None
                except Exception:
                    pass
                if got_callback:
                    break
                # After 120s timeout, fall through to next consent_try (which
                # may reload — acceptable at this point).
                continue

            for _ in range(3):
                clicked = click_first_visible(page, [
                    'button:has-text("Continue")',
                    'button:has-text("Authorize")',
                    'button:has-text("Allow")',
                    'button:has-text("Accept")',
                    'button:has-text("Yes")',
                ], timeout=800)
                if not clicked:
                    break
                print(f"    [consent] Clicked continue/authorize (try {consent_try + 1}/3)")
                wait_a_bit(page, 300)
                if "localhost" in page.url and "/auth/callback" in page.url:
                    break

            try:
                current = page.url
                body_text = page.locator("body").inner_text(timeout=500).lower()
                if "localhost" in current and "/auth/callback" in current:
                    parsed = urlparse(current)
                    params = parse_qs(parsed.query)
                    callback_state = params.get("state", [None])[0]
                    if callback_state == state:
                        result.code = params.get("code", [None])[0]
                        result.state = callback_state
                        result.error = params.get("error", [None])[0]
                        got_callback = result.code is not None or result.error is not None
                        if got_callback:
                            break
                if "invalid_state" in current.lower() or "invalid_state" in body_text or "session ended" in body_text:
                    return None, "RETRYABLE_INVALID_STATE: OAuth session ended/invalid_state", verifier, state
            except Exception:
                pass

            if consent_try < 2:
                print(f"    [retry] Callback not received, retrying final consent ({consent_try + 2}/3)")
                # Country check in retry loop
                try:
                    select_country_region(page)
                except Exception:
                    pass
                try:
                    page.reload(wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
                wait_a_bit(page, 600)

        if not got_callback:
            # Check if page URL is already on callback
            try:
                current = page.url
                if "localhost" in current and "/auth/callback" in current:
                    parsed = urlparse(current)
                    params = parse_qs(parsed.query)
                    result.code = params.get("code", [None])[0]
                    result.state = params.get("state", [None])[0]
                    got_callback = result.code is not None
            except:
                pass

        if not got_callback:
            return None, "Timeout waiting for callback after 3 consent attempts", verifier, state

        if result.error:
            return None, f"OAuth error: {result.error}", verifier, state

        if not result.code:
            return None, "No authorization code received", verifier, state

        if result.state and result.state != state:
            return None, "OAuth callback state did not match this worker", verifier, state

        # Exchange code for tokens
        tokens, err = exchange_code(result.code, verifier)
        if err:
            return None, f"Token exchange: {err}", verifier, state

        return tokens, None, verifier, state

    finally:
        unregister_callback(state)


def select_country_region(page, target_country="Cambodia"):
    """
    Handle country/region selection prompt (e.g. Phone number required screen).
    In this screen, OpenAI shows a button with current country code (e.g. "United States (+1)").
    Clicking it opens a list/menu with items like "Cambodia (+855)" or search filter.
    """
    try:
        # Check if we are on a country or phone requirement screen
        screen_detected = False
        # The phone screen can appear well after the 2FA redirect; keep polling
        # long enough for OpenAI's client-side route and React UI to settle.
        for _ in range(80):
            try:
                body = page.locator("body").inner_text(timeout=300).lower()
                if any(m in body for m in ("phone number", "country", "region", "số điện thoại", "quốc gia")):
                    screen_detected = True
                    break
            except Exception:
                pass
            time.sleep(0.25)

        if not screen_detected:
            return False

        print(f"    [country] Country/Phone screen detected, selecting {target_country}...")

        # Strategy A: Native <select> element
        for sel in ['select[name*="country" i]', 'select[id*="country" i]', 'select']:
            loc = page.locator(sel).first
            if loc.is_visible():
                try:
                    loc.select_option(label=target_country, timeout=1000)
                    print(f"    [country] Selected '{target_country}' via <select>")
                    return True
                except Exception:
                    pass

        # Strategy B: Phone Country Picker Button. The real OpenAI screen uses
        # a visible text control such as `United States (+1)`; it is not always
        # a button or a role=combobox, so inspect visible text-bearing elements.
        picker_candidates = [
            'button[aria-haspopup="listbox"]',
            'button[aria-haspopup="menu"]',
            '[role="combobox"]',
            'button:has-text("(+")',
            'text=/\\(\\+\\d{1,4}\\)/',
        ]
        opened = False
        for selector in picker_candidates:
            for candidate in page.locator(selector).all():
                try:
                    if not candidate.is_visible():
                        continue
                    label = candidate.inner_text(timeout=400).strip()
                    if not re.search(r"\(\+\d{1,4}\)", label):
                        continue
                    if target_country.lower() in label.lower():
                        print(f"    [country] '{target_country}' is already selected")
                        return True
                    candidate.click()
                    opened = True
                    print("    [country] Clicked OpenAI country-code picker")
                    break
                except Exception:
                    continue
            if opened:
                break

        if not opened:
            return False

        # Wait for the React Aria listbox to be visible.
        try:
            listbox = page.locator('[role="listbox"]').first
            listbox.wait_for(state="visible", timeout=4000)
        except Exception:
            listbox = None
            time.sleep(0.5)

        # Strategy C1: React Aria virtualizer auto-scroll & click via DOM evaluate.
        # Live OpenAI DOM uses a virtual list (height 9320px, 40px/item) inside [role="listbox"].
        # Items outside the visible window are NOT rendered in DOM until scrolled.
        # By scrolling listbox.scrollTop programmatically in steps, the virtualizer
        # mounts the target item into DOM so it can be clicked immediately.
        try:
            clicked_via_scroll = page.evaluate("""
                (targetKey) => {
                    const listbox = document.querySelector('[role="listbox"]');
                    if (!listbox) return false;

                    // Check if already in DOM first
                    let opt = listbox.querySelector(`[role="option"][data-key="${targetKey}"], [data-key="${targetKey}"]`);
                    if (opt) {
                        opt.click();
                        return true;
                    }

                    // Cambodia (KH) is around index 35 -> top: 1360px.
                    // Scan by scrolling scrollTop in 300px increments up to 3000px.
                    for (let top = 200; top <= 3000; top += 300) {
                        listbox.scrollTop = top;
                        opt = listbox.querySelector(`[role="option"][data-key="${targetKey}"], [data-key="${targetKey}"]`);
                        if (!opt) {
                            // Also try text match
                            const allOpts = Array.from(listbox.querySelectorAll('[role="option"]'));
                            opt = allOpts.find(o => (o.innerText || '').toLowerCase().includes('cambodia'));
                        }
                        if (opt) {
                            opt.click();
                            return true;
                        }
                    }
                    return false;
                }
            """, "KH")
            if clicked_via_scroll:
                print("    [country] Successfully selected Cambodia (+855) via virtual list scroll")
                time.sleep(0.5)
                return True
        except Exception as _e:
            pass

        # Strategy C2: Fast Playwright scroll & click by data-key="KH"
        data_key = "KH"
        for scroll_top in [0, 400, 800, 1200, 1360, 1600, 2000]:
            try:
                page.evaluate(f"el => el.scrollTop = {scroll_top}", listbox.element_handle())
                time.sleep(0.1)
                opt = page.locator(f'[role="option"][data-key="{data_key}"]').first
                if opt.count() > 0 and opt.is_visible():
                    opt.click()
                    print(f"    [country] Clicked Cambodia via data-key={data_key} at scroll={scroll_top}")
                    time.sleep(0.4)
                    return True
            except Exception:
                pass

        # Strategy C3: Keyboard type-ahead ("cam")
        if listbox is not None:
            try:
                if listbox.is_visible():
                    listbox.focus()
                    listbox.type("cam", delay=100)
                    time.sleep(0.3)
                    opt = page.locator('[role="option"][data-key="KH"]').first
                    if opt.count() > 0 and opt.is_visible():
                        opt.click()
                        print("    [country] Clicked Cambodia via keyboard type-ahead")
                        time.sleep(0.4)
                        return True
            except Exception:
                pass

        # OpenAI may virtualize the country list. Prefer its search field so
        # Cambodia is rendered before looking for the option. Never target the
        # phone-number input here.
        for search_sel in [
            'input[placeholder*="search" i]',
            'input[placeholder*="find" i]',
            'input[aria-label*="search" i]',
            'input[type="search"]',
        ]:
            try:
                search = page.locator(search_sel).first
                if search.is_visible():
                    search.fill(target_country)
                    time.sleep(0.4)
                    break
            except Exception:
                continue

        # Strategy C: Find the actual option after the picker is open. Do not
        # type into the phone field and do not click Continue/Submit here.
        cambodia_selectors = [
            '[role="option"][data-key="KH"]',
            'text="Cambodia (+855)"',
            '[role="option"]:has-text("Cambodia (+855)")',
            '[role="menuitem"]:has-text("Cambodia (+855)")',
            'li:has-text("Cambodia (+855)")',
            'button:has-text("Cambodia (+855)")',
            'text="Cambodia"',
            '[role="option"]:has-text("Cambodia")',
            '[role="menuitem"]:has-text("Cambodia")',
            'li:has-text("Cambodia")',
        ]
        for opt_sel in cambodia_selectors:
            try:
                opt = page.locator(opt_sel).first
                if opt.count() > 0:
                    opt.scroll_into_view_if_needed(timeout=1000)
                    if opt.is_visible():
                        opt.click()
                        print(f"    [country] Successfully clicked '{target_country}' option")
                        time.sleep(0.4)
                        return True
            except Exception:
                continue

        # List may be long — scroll Cambodia into view and click
        for candidate in page.locator(f'text={target_country}').all():
            try:
                candidate.scroll_into_view_if_needed(timeout=1000)
                if candidate.is_visible():
                    candidate.click()
                    print(f"    [country] Scrolled & clicked '{target_country}'")
                    return True
            except Exception:
                continue

        # Picker opened but Cambodia not found in list — leave picker open for
        # the user to see; do not submit anything.
        print(f"    [!] Country picker opened but '{target_country}' option not found")
        return False

    except Exception as e:
        print(f"    [!] select_country_region error: {e}")
    return False


def select_chatgpt_workspace(page):
    """Choose the personal workspace when ChatGPT asks after authentication."""
    personal_selectors = [
        'button:has-text("Personal account")',
        '[role="button"]:has-text("Personal account")',
        'a:has-text("Personal account")',
        'text="Personal account"',
        'button:has-text("Tài khoản cá nhân")',
        '[role="button"]:has-text("Tài khoản cá nhân")',
    ]
    for selector in personal_selectors:
        try:
            locator = page.locator(selector).first
            if locator.is_visible():
                locator.click()
                return "personal"
        except Exception:
            pass
    return None


def verify_personal_account_label(page, timeout=15000):
    """Return whether the final ChatGPT UI exposes a visible exact account label."""
    handle = None
    try:
        # ChatGPT reuses the caption utility classes across the sidebar. Taking
        # the first class match can therefore read an unrelated label and return
        # immediately before the account footer has rendered. Poll the live DOM
        # for exact visible text instead; this is independent of class-name and
        # nesting changes while still rejecting partial/hidden matches.
        handle = page.wait_for_function(
            """
            expected => Array.from(document.querySelectorAll('body *')).some(element => {
              const text = (element.innerText || '').trim();
              const style = window.getComputedStyle(element);
              const visible = Boolean(
                element.offsetWidth || element.offsetHeight || element.getClientRects().length
              ) && style.visibility === 'visible' && style.opacity !== '0';
              return visible && text === expected;
            })
            """,
            arg="Personal account",
            polling=250,
            timeout=timeout,
        )
        return True
    except Exception:
        return False
    finally:
        try:
            if handle:
                handle.dispose()
        except Exception:
            pass


def emit_web_phase(email, index, stage, message):
    """Emit a safe, user-visible progress event without credentials."""
    emit_event("PHASE", email, index=index, stage=stage, message=message)


def retry_web_email_step(page, email, password, index=0):
    """Retry one email submission when ChatGPT unexpectedly returns to step one."""
    email_input = fill_first_visible(page, [
        'input[name="email"]',
        'input[type="email"]',
        'input[name="username"]',
        'input[id*="email" i]',
        'input[placeholder*="email" i]',
        'input[autocomplete="username"]',
    ], email, timeout=2500)
    if not email_input:
        return None

    emit_web_phase(email, index, "email_retry", "Trang email xuất hiện lại, đang gửi lại một lần")
    if not click_first_visible(page, [
        'button[type="submit"]',
        'button:has-text("Continue")',
        'button:has-text("Next")',
        'button:has-text("Tiếp tục")',
        'button:has-text("Log in")',
    ], timeout=1500):
        email_input.press("Enter")

    return fill_first_visible(page, [
        'input[name="password"]',
        'input[type="password"]',
        'input[id*="password" i]',
        'input[autocomplete="current-password"]',
        'input[placeholder*="password" i]',
        'input[placeholder*="mật khẩu" i]',
    ], password, timeout=15000)


def login_chatgpt_web(page, email, password, totp_secret, attempt=1, index=0):
    """Log into the normal ChatGPT website without Codex OAuth or token import."""
    emit_web_phase(email, index, "open_login", "Đang mở trang đăng nhập ChatGPT")
    page.goto(CHATGPT_LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
    wait_a_bit(page, 700)
    debug_page(page, "web_01_open_login")

    # Some ChatGPT landing variants show a Log in button before the email form.
    if not any(page.locator(selector).first.is_visible() for selector in [
        'input[name="email"]', 'input[type="email"]', 'input[name="username"]'
    ]):
        click_first_visible(page, [
            'button:has-text("Log in")',
            'a:has-text("Log in")',
            'button:has-text("Đăng nhập")',
            'a:has-text("Đăng nhập")',
        ], timeout=5000)
        wait_a_bit(page, 700)

    email_input = fill_first_visible(page, [
        'input[name="email"]',
        'input[type="email"]',
        'input[name="username"]',
        'input[id*="email" i]',
        'input[placeholder*="email" i]',
        'input[autocomplete="username"]',
    ], email, timeout=15000)
    if not email_input:
        debug_page(page, "web_02_email_not_found")
        return False, "Email input not found"

    if not click_first_visible(page, [
        'button[type="submit"]',
        'button:has-text("Continue")',
        'button:has-text("Next")',
        'button:has-text("Tiếp tục")',
        'button:has-text("Log in")',
    ], timeout=1500):
        email_input.press("Enter")
    emit_web_phase(email, index, "email", "Đã gửi email, đang chờ ô mật khẩu")

    password_input = fill_first_visible(page, [
        'input[name="password"]',
        'input[type="password"]',
        'input[id*="password" i]',
        'input[autocomplete="current-password"]',
        'input[placeholder*="password" i]',
        'input[placeholder*="mật khẩu" i]',
    ], password, timeout=15000)
    if not password_input:
        password_input = retry_web_email_step(page, email, password, index=index)
        if not password_input:
            debug_page(page, "web_03_password_not_found")
            return False, "Password input not found after retrying the email step"

    if not click_first_visible(page, [
        'button[type="submit"]',
        'button:has-text("Continue")',
        'button:has-text("Next")',
        'button:has-text("Log in")',
        'button:has-text("Sign in")',
        'button:has-text("Đăng nhập")',
    ], timeout=1500):
        password_input.press("Enter")
    emit_web_phase(email, index, "password", "Đã gửi mật khẩu")
    wait_a_bit(page, 1000)

    if totp_secret:
        otp_input = fill_first_visible(page, [
            'input[name="code"]',
            'input[inputmode="numeric"]',
            'input[autocomplete="one-time-code"]',
            'input[id*="code" i]',
            'input[placeholder*="code" i]',
            'input[placeholder*="verification" i]',
        ], "", timeout=10000)
        if otp_input:
            emit_web_phase(email, index, "two_factor", "Đang xác minh mã 2FA")
            try:
                otp_code = get_2fa_code(totp_secret, attempt=attempt)
            except Exception as error:
                return False, "Invalid 2FA secret: {}".format(error)
            otp_input.fill(otp_code)
            if not click_first_visible(page, [
                'button[type="submit"]',
                'button:has-text("Continue")',
                'button:has-text("Verify")',
                'button:has-text("Next")',
            ], timeout=2000):
                otp_input.press("Enter")
            wait_a_bit(page, 1500)

    # --- Country/region selection (appears after 2FA on some accounts) ---
    try:
        if select_country_region(page):
            emit_web_phase(email, index, "country", "Đã chọn quốc gia Cambodia")
            wait_a_bit(page, 800)
    except Exception:
        pass

    deadline = time.time() + 60
    workspace_seen = False
    manual_verification_seen = False
    country_attempted = False
    phone_wait_extended = False
    while time.time() < deadline:
        # Detect phone verification screen and retry country picker
        try:
            body_now = page.locator("body").inner_text(timeout=500).lower()
        except Exception:
            body_now = ""

        is_phone = any(marker in body_now for marker in (
            "phone number required", "add your phone number",
            "verify your phone", "phone verification",
            "enter your phone",
        )) or "/phone" in page.url.lower() or "/add-phone" in page.url.lower()

        if is_phone:
            # Extend deadline so user has time to enter phone + OTP manually
            if not phone_wait_extended:
                deadline = time.time() + 180
                phone_wait_extended = True
                emit_web_phase(email, index, "phone_wait",
                    "Đang chờ nhập số điện thoại và OTP trên Chrome")
            # Try country selection up to 3 times total
            if not country_attempted:
                try:
                    if select_country_region(page):
                        country_attempted = True
                        emit_web_phase(email, index, "country", "Đã chọn quốc gia Cambodia (+855)")
                        wait_a_bit(page, 800)
                except Exception:
                    pass
            # Don't do any other automation while on phone screen
            time.sleep(1)
            continue

        selected_workspace = None if workspace_seen else select_chatgpt_workspace(page)
        if selected_workspace:
            workspace_seen = True
            emit_web_phase(email, index, "workspace", "Đã chọn Personal account")
            wait_a_bit(page, 900)
            continue

        current_url = page.url.lower()
        if current_url.startswith("https://chatgpt.com/") and "/auth/" not in current_url:
            emit_web_phase(email, index, "authenticated", "Đăng nhập ChatGPT thành công")
            return True, None
        try:
            body_text = page.locator("body").inner_text(timeout=500).lower()
            if not totp_secret and any(message in body_text for message in [
                "verification code", "authenticator app", "mã xác minh",
            ]):
                return False, "2FA is required but no 2FA secret was provided"
            if not manual_verification_seen and any(message in body_text for message in [
                "verify you are human", "checking your browser", "security check",
                "xác minh bạn là con người",
            ]):
                manual_verification_seen = True
                emit_web_phase(email, index, "manual_check", "Cần xác minh thủ công trên cửa sổ Chrome")
            if any(message in body_text for message in [
                "incorrect password", "wrong password", "invalid code",
                "mật khẩu không đúng", "mã không hợp lệ",
            ]):
                return False, "ChatGPT rejected the login credentials or verification code"
        except Exception:
            pass
        time.sleep(0.25)

    debug_page(page, "web_04_login_timeout")
    if workspace_seen:
        return False, "Workspace selection did not complete"
    if manual_verification_seen:
        return False, "Manual browser verification was not completed in time"
    return False, "Timeout waiting for ChatGPT web login"


# ---------- Main ----------
def parse_accounts(accounts_file):
    accounts = []
    with open(accounts_file, "r", encoding="utf-8") as source:
        for line in source:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            email = parts[0].strip()
            password = parts[1].strip() if len(parts) > 1 else ""
            totp = parts[2].strip() if len(parts) > 2 else ""
            if email and password:
                accounts.append((email, password, totp))
    return accounts


def emit_event(kind, email, **payload):
    """Emit machine-readable progress while keeping normal logs readable."""
    clean = lambda value: str(value).replace("|", "/").replace("\r", " ").replace("\n", " ")[:800]
    fields = ["EVENT", clean(kind), clean(email)]
    fields.extend("{}={}".format(key, clean(value)) for key, value in payload.items())
    line = ("|".join(fields) + "\n").encode("utf-8", errors="replace")
    with _event_output_lock:
        try:
            sys.stdout.flush()
            os.write(sys.stdout.fileno(), line)
        except (AttributeError, OSError, ValueError):
            print(line.decode("utf-8", errors="replace"), end="", flush=True)


def login_one_account(index, total, account, headed, slow):
    email, password, totp_secret = account
    emit_event("START", email, index=index, total=total)
    print("[{}/{}] {}".format(index, total, email), flush=True)
    last_error = ""
    launch_kwargs = dict(
        headless=not headed,
        slow_mo=500 if slow else 0,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-save-password-bubble",
            "--disable-features=PasswordManagerOnboarding,PasswordLeakDetection",
        ],
    )

    for account_attempt in range(3):
        pw = None
        browser = None
        context = None
        keep_session = False
        try:
            pw = sync_playwright().start()
            try:
                browser = pw.chromium.launch(channel="chrome", **launch_kwargs)
                if account_attempt == 0:
                    print("    Browser: Google Chrome | shared callback={}".format(REDIRECT_URI), flush=True)
            except Exception as error:
                print("    [!] Chrome unavailable, fallback Chromium: {}".format(error), flush=True)
                browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )
            page = context.new_page()
            if account_attempt:
                print("    [retry] Restarted browser ({}/3)".format(account_attempt + 1), flush=True)
            tokens, error, _, _ = login_account(page, email, password, totp_secret, headed, attempt=account_attempt + 1)
            if error:
                last_error = error
                retryable = "RETRYABLE_INVALID_STATE" in error or "Timeout waiting for callback" in error or "RETRYABLE_2FA" in error
                if retryable and account_attempt < 2:
                    print("    [retry] {}".format(error), flush=True)
                    continue
                clean_error = error.replace("RETRYABLE_INVALID_STATE: ", "").replace("RETRYABLE_2FA: ", "")
                print("    ❌ {}".format(clean_error), flush=True)
                emit_event("ERROR", email, error=clean_error)
                return {"email": email, "status": "error", "error": clean_error}

            conn = tokens_to_connection(tokens)
            actual_email = conn.get("email") or email
            plan = conn.get("providerSpecificData", {}).get("chatgptPlanType", "?")
            has_rt = bool(conn.get("refreshToken"))
            print("    ✅ {} | plan={} | rt={}".format(actual_email, plan, "yes" if has_rt else "NO!"), flush=True)
            response, import_error = import_to_9router(conn)

            if response:
                print("IMPORT_OK|{}|{}|{}|{}".format(actual_email, plan, "yes" if has_rt else "no", response.get("sqlitePath", "")), flush=True)
                emit_event("SUCCESS", actual_email, plan=plan, refresh="yes" if has_rt else "no")
                return {"email": actual_email, "status": "success", "plan": plan, "hasRefreshToken": has_rt, "imported": True}

            message = "OAuth OK but 9router import failed: {}".format(import_error or "unknown error")
            print("IMPORT_FAIL|{}|{}".format(actual_email, import_error or "unknown import error"), flush=True)
            emit_event("ERROR", actual_email, error=message)
            return {"email": actual_email, "status": "error", "error": message, "imported": False}
        except Exception as error:
            last_error = str(error)
            if account_attempt < 2:
                print("    [retry] Exception, restarting browser: {}".format(error), flush=True)
                continue
            print("    ❌ Exception: {}".format(error), flush=True)
            emit_event("ERROR", email, error=last_error)
            return {"email": email, "status": "error", "error": last_error}
        finally:
            if not keep_session:
                try:
                    if context:
                        context.close()
                except Exception:
                    pass
                try:
                    if browser:
                        browser.close()
                except Exception:
                    pass
                try:
                    if pw:
                        pw.stop()
                except Exception:
                    pass
    emit_event("ERROR", email, error=last_error or "Unknown login error")
    return {"email": email, "status": "error", "error": last_error or "Unknown login error"}


def login_codex_authorization(page, email, password, totp_secret, index=0):
    """Complete Codex OAuth in this browser and leave the authenticated tab open."""
    auth_url, _verifier, state = build_codex_auth_url()

    def callback_received():
        parsed = urlparse(page.url)
        if parsed.hostname not in ("localhost", "127.0.0.1") or parsed.path != "/auth/callback":
            return False
        params = parse_qs(parsed.query)
        return params.get("state", [None])[0] == state and bool(params.get("code", [None])[0]) and not params.get("error")
    def on_auth_host():
        parsed = urlparse(page.url)
        return parsed.scheme == "https" and parsed.netloc == "auth.openai.com"

    def auth_stage():
        # Do not treat merely visiting a URL as successful authentication.
        if not on_auth_host():
            return None
        path = urlparse(page.url).path
        for kind, selector in (
            ("otp", 'input[autocomplete="one-time-code"], input[name="code"]'),
            ("password", 'input[type="password"]'),
            ("email", 'input[type="email"], input[name="email"], input[name="username"]'),
        ):
            field = page.locator(selector).first
            if field.is_visible():
                return kind, field
        if "consent" in path or "authorize" in path:
            for selector in ('button:has-text("Allow")', 'button:has-text("Authorize")', 'button:has-text("Continue")'):
                if page.locator(selector).first.is_visible():
                    return "consent"
        return None

    try:
        emit_web_phase(email, index, "codex_authorize", "Đang mở OAuth authorize của Codex trên auth.openai.com")
        page.goto(auth_url, wait_until="domcontentloaded", timeout=45000)
        sent = set()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not _session_shutdown_event.is_set():
            stage = auth_stage()
            if stage == "consent":
                emit_web_phase(email, index, "codex_consent", "Đang xác nhận quyền Codex trên auth.openai.com")
                clicked = False
                for selector in ('button:has-text("Allow")', 'button:has-text("Authorize")', 'button:has-text("Continue")'):
                    button = page.locator(selector).first
                    if button.is_visible():
                        clicked = True
                        try:
                            button.click()
                        except Exception:
                            # localhost:1455 intentionally has no listener in Codex mode;
                            # Chromium may report the refused navigation after updating page.url.
                            pass
                        break
                if not clicked:
                    break
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not _session_shutdown_event.is_set():
                    if callback_received():
                        emit_web_phase(email, index, "codex_authenticated", "Codex OAuth đã xác thực; giữ nguyên tab để bạn thao tác")
                        return True
                    page.wait_for_timeout(250)
                if callback_received():
                    emit_web_phase(email, index, "codex_authenticated", "Codex OAuth đã xác thực; giữ nguyên tab để bạn thao tác")
                    return True
                break
            if stage:
                kind, field = stage
                if kind not in sent:
                    if kind == "otp" and not totp_secret:
                        break
                    value = email if kind == "email" else password if kind == "password" else pyotp.TOTP(
                        re.sub(r"\s+", "", totp_secret).upper()
                    ).now()
                    if not on_auth_host():
                        break
                    field.fill(value)
                    if not on_auth_host():
                        break
                    # Submit only the credential form, never a generic consent button.
                    field.press("Enter")
                    sent.add(kind)
                    emit_web_phase(email, index, "codex_" + kind,
                                   {"email": "Đã gửi email OAuth", "password": "Đã gửi mật khẩu trên trang auth", "otp": "Đã gửi mã 2FA"}[kind])
            current = urlparse(page.url)
            if current.hostname in ("localhost", "127.0.0.1"):
                if callback_received():
                    return True
                break
            if current.scheme != "https" or current.netloc != "auth.openai.com":
                break  # Hand off SSO/verification/other pages without sending credentials.
            page.wait_for_timeout(250)
    except Exception:
        pass  # URL, form values, authorization codes and screenshots never enter logs.
    emit_web_phase(email, index, "codex_manual", "Giữ tab OAuth để bạn kiểm tra hoặc hoàn tất xác thực Codex.")
    return False


def login_web_one_account(
    index,
    total,
    account,
    slow=False,
    open_link=DEFAULT_WEB_LINK,
    report_terminal=None,
    attempt=1,
    layout_index=None,
    layout_total=None,
    reload_after=None,
    codex_web=False,
):
    """Run one clean direct-login attempt and retain a successful browser."""
    if codex_web:
        open_link, reload_after = "", None
    email, password, totp_secret = account
    emit_event("START", email, index=index, total=total, attempt=attempt)
    print("[{}/{}] {} | attempt {}".format(index, total, email, attempt), flush=True)
    layout_index = int(layout_index or index)
    layout_total = int(layout_total or total)
    window_bounds = calculate_window_bounds(layout_index, layout_total)
    launch_kwargs = dict(
        headless=False,
        slow_mo=500 if slow else 0,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-save-password-bubble",
            "--disable-features=PasswordManagerOnboarding,PasswordLeakDetection",
            "--window-position={},{}".format(window_bounds["left"], window_bounds["top"]),
            "--window-size={},{}".format(window_bounds["width"], window_bounds["height"]),
        ],
    )
    pw = None
    browser = None
    context = None
    session_retained = False
    terminal_reported = False

    def finish(result):
        nonlocal terminal_reported
        if result.get("status") == "success" and report_terminal and not terminal_reported:
            terminal_reported = True
            report_terminal(result)
        return result

    try:
        emit_web_phase(email, index, "browser_start", "Đang khởi tạo Chrome riêng")
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(channel="chrome", **launch_kwargs)
            print("    Browser: Google Chrome | {}".format("Codex OAuth" if codex_web else "direct ChatGPT web"), flush=True)
        except Exception as error:
            print("    [!] Chrome unavailable, fallback Chromium: {}".format(error), flush=True)
            browser = pw.chromium.launch(**launch_kwargs)
        emit_web_phase(email, index, "browser_ready", "Chrome đã sẵn sàng")
        context = browser.new_context(**build_web_context_options(window_bounds))
        page = context.new_page()
        if apply_browser_window_bounds(context, page, window_bounds):
            emit_web_phase(
                email,
                index,
                "window_layout",
                "Đã xếp cửa sổ vào ô {}/{} ({}×{})".format(
                    layout_index,
                    layout_total,
                    window_bounds["width"],
                    window_bounds["height"],
                ),
            )
        codex_opened = False
        if codex_web:
            codex_opened = login_codex_authorization(page, email, password, totp_secret, index=index)
            ok, error = True, None  # Retain even a manual-check/error page; never replay OAuth.
        else:
            ok, error = login_chatgpt_web(page, email, password, totp_secret, attempt=attempt, index=index)
        if not ok:
            print("    ❌ {}".format(error), flush=True)
            return finish({"email": email, "status": "error", "error": error, "index": index})

        link_page = None
        link_opened = False
        link_error = None
        post_login_error = ""
        if open_link:
            try:
                emit_web_phase(email, index, "open_link", "Đang mở tab phụ")
                link_page = context.new_page()
                # `load` waits for the secondary page's browser load event. A
                # network-idle wait is intentionally avoided because many
                # pages keep long-lived requests open.
                link_page.goto(open_link, wait_until="load", timeout=45000)
                link_opened = True
                print("WEB_LINK_OPEN|{}|{}".format(email, open_link), flush=True)
                emit_web_phase(email, index, "link_ready", "Tab phụ đã tải xong")
            except Exception as error:
                link_error = str(error)
                post_login_error = "link_error"
                print("WEB_LINK_FAIL|{}|{}|{}".format(email, open_link, link_error.replace("|", "/")), flush=True)
                emit_web_phase(email, index, "link_error", "Tab phụ mở không thành công")
            finally:
                # Opening a new page can move focus away from ChatGPT. Return
                # to the original page before any delay, reload, or account
                # validation so each worker keeps its own deterministic order.
                try:
                    page.bring_to_front()
                    emit_web_phase(email, index, "main_tab_ready", "Đã quay lại tab chính ChatGPT")
                except Exception:
                    post_login_error = post_login_error or "main_tab_error"
                    emit_web_phase(email, index, "main_tab_error", "Không đưa được tab chính lên trước; vẫn giữ phiên")
        reloaded = False
        reload_error = ""
        if reload_after is not None and not post_login_error:
            emit_web_phase(email, index, "reload_wait", "Chờ {} giây để reload tab ChatGPT một lần".format(reload_after))
            if not _session_shutdown_event.wait(reload_after):
                try:
                    page.reload(wait_until="load", timeout=45000)
                    reloaded = True
                    print("WEB_CHATGPT_RELOAD|{}".format(email), flush=True)
                    emit_web_phase(email, index, "reloaded", "Đã reload tab ChatGPT một lần; giữ tab để bạn sử dụng")
                except Exception as error:
                    reload_error = type(error).__name__
                    post_login_error = "reload_error"
                    emit_web_phase(email, index, "reload_error", "Không reload được; giữ nguyên phiên đăng nhập, không thử lại")
            else:
                post_login_error = "cancelled"
        if not codex_web and _session_shutdown_event.is_set():
            post_login_error = post_login_error or "cancelled"
        personal_account_verified = False
        if not codex_web and not post_login_error:
            emit_web_phase(email, index, "personal_account_check", "Đang kiểm tra nhãn Personal account")
            personal_account_verified = verify_personal_account_label(page)
            emit_web_phase(email, index,
                           "personal_account_verified" if personal_account_verified else "personal_account_unverified",
                           "Đã xác minh Personal account" if personal_account_verified else "Chưa xác minh Personal account")
        print("WEB_OPEN|{}|{}".format(email, "Codex OAuth" if codex_web else CHATGPT_URL), flush=True)
        command_queue = queue.Queue()
        session_entry = {
            "email": email,
            "index": index,
            "layout_index": layout_index,
            "layout_total": layout_total,
            "pw": pw,
            "browser": browser,
            "context": context,
            "page": page,
            "link_page": link_page,
            "commands": command_queue,
        }
        with _kept_sessions_lock:
            session_key = "web:{}#{}".format(email.lower(), threading.get_ident())
            _kept_browser_sessions[session_key] = session_entry
            session_retained = True
        result = {
            "email": email,
            "index": index,
            "status": "success",
            "webOpened": True,
            "linkOpened": link_opened,
            "linkUrl": open_link,
            "linkError": link_error or "",
            "chatgptReloaded": reloaded,
            "chatgptReloadError": reload_error,
            "personalAccountVerified": personal_account_verified,
            "postLoginError": post_login_error,
            "codexOpened": codex_opened,
        }
        emit_event(
            "SUCCESS",
            email,
            index=index,
            web="yes",
            link="yes" if link_opened else "no",
            reloaded="yes" if result["chatgptReloaded"] else "no",
            personal="yes" if result["personalAccountVerified"] else "no",
            codex="yes" if codex_opened else "no",
            flow_error=post_login_error,
        )
        finish(result)

        # Keep Playwright on the same thread that created it. The queue slot was
        # already released by finish(), so the next account can start now.
        if report_terminal:
            service_retained_browser_commands(session_key, session_entry)
            session_retained = False
        return result
    except Exception as error:
        print("    ❌ Exception: {}".format(error), flush=True)
        return finish({"email": email, "status": "error", "error": str(error), "index": index})
    finally:
        if not session_retained:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass


def run_web_login_queue(
    accounts,
    workers,
    slow=False,
    open_link=DEFAULT_WEB_LINK,
    job=None,
    retry_delay=3,
    retry_max_delay=30,
    reload_after=None,
    codex_web=False,
):
    """Retry failed accounts until all succeed while preserving bounded concurrency."""
    if not accounts:
        return []
    if workers < 1:
        raise ValueError("workers must be a positive integer")

    login_job = job or login_web_one_account
    total = len(accounts)
    active_limit = min(workers, total)
    terminal_results = queue.Queue()
    report_lock = threading.Lock()
    reported_attempts = set()
    results = [None] * total
    initial_pending = deque(range(total))
    retry_pending = []
    retry_counts = [0] * total

    def report_result(result_index, attempt, layout_index, result):
        attempt_key = (result_index, attempt)
        with report_lock:
            if attempt_key in reported_attempts:
                return
            reported_attempts.add(attempt_key)
        terminal_results.put((result_index, attempt, layout_index, result))

    def run_one(result_index, account, attempt, layout_index):
        email = account[0]
        try:
            common_args = (
                result_index + 1,
                total,
                account,
                slow,
                open_link,
                lambda value: report_result(result_index, attempt, layout_index, value),
                attempt,
            )
            if login_job is login_web_one_account:
                result = login_job(
                    *common_args,
                    layout_index=layout_index,
                    # Every successful account remains open. Use its stable
                    # account slot instead of recycling the worker slot, or a
                    # later account would launch on top of a retained window.
                    layout_total=total,
                    reload_after=reload_after,
                    codex_web=codex_web,
                )
            else:
                result = login_job(*common_args)
            if result is not None:
                report_result(result_index, attempt, layout_index, result)
        except Exception as error:
            print("    ❌ Exception: {}".format(error), flush=True)
            report_result(result_index, attempt, layout_index, {
                "email": email,
                "index": result_index + 1,
                "status": "error",
                "error": str(error),
            })

    def start_account(result_index, attempt, layout_index):
        account = accounts[result_index]
        thread = threading.Thread(
            target=run_one,
            args=(result_index, account, attempt, layout_index),
            name="chatgpt-web-{}-attempt-{}".format(result_index + 1, attempt),
            daemon=True,
        )
        thread.start()

    for result_index, account in enumerate(accounts):
        emit_event("QUEUED", account[0], index=result_index + 1, total=total)

    completed = 0
    active_attempts = 0
    while completed < total:
        now = time.monotonic()
        while active_attempts < active_limit:
            if initial_pending:
                result_index = initial_pending.popleft()
                attempt = 1
            elif retry_pending and retry_pending[0][0] <= now:
                _, result_index, attempt = heapq.heappop(retry_pending)
            else:
                break
            layout_index = result_index + 1
            start_account(result_index, attempt, layout_index)
            active_attempts += 1

        wait_timeout = None
        if active_attempts < active_limit and retry_pending:
            wait_timeout = max(0.001, retry_pending[0][0] - time.monotonic())
        try:
            result_index, attempt, layout_index, result = terminal_results.get(timeout=wait_timeout)
        except queue.Empty:
            continue

        active_attempts = max(0, active_attempts - 1)
        if result.get("status") == "success" or codex_web:
            if codex_web and result.get("status") != "success":
                emit_event("ERROR", accounts[result_index][0], index=result_index + 1,
                           error="Xác thực Codex chưa hoàn tất; hãy kiểm tra tab đang mở")
            results[result_index] = result
            completed += 1
            continue

        # Failed attempts have fully returned and cleaned up, so their
        # de-duplication marker no longer needs to stay in memory.
        with report_lock:
            reported_attempts.discard((result_index, attempt))

        retry_counts[result_index] += 1
        growth = 2 ** min(retry_counts[result_index] - 1, 20)
        delay = min(
            max(0, float(retry_delay)) * growth,
            max(0, float(retry_max_delay)),
        )
        next_attempt = attempt + 1
        emit_event(
            "RETRY",
            accounts[result_index][0],
            index=result_index + 1,
            attempt=attempt,
            nextAttempt=next_attempt,
            delay="{:.1f}".format(delay),
            error=result.get("error") or "Login attempt failed",
        )
        heapq.heappush(
            retry_pending,
            (time.monotonic() + delay, result_index, next_attempt),
        )
    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: python auto_login.py accounts.txt [--headed] [--slow] [--workers N] [--web-only]")
        sys.exit(1)
    accounts_file = sys.argv[1]
    headed = "--headed" in sys.argv or "--show" in sys.argv
    slow = "--slow" in sys.argv
    codex_web = "--codex-web" in sys.argv
    web_only = "--web-only" in sys.argv or codex_web
    reload_after = None
    if "--reload-after" in sys.argv:
        try:
            reload_after = int(sys.argv[sys.argv.index("--reload-after") + 1])
            if not 1 <= reload_after <= 3600:
                raise ValueError()
        except (ValueError, IndexError):
            print("[!] --reload-after requires an integer from 1 to 3600 seconds")
            sys.exit(1)
    open_link = DEFAULT_WEB_LINK
    if "--open-link" in sys.argv or "--web-link" in sys.argv:
        link_flag = "--open-link" if "--open-link" in sys.argv else "--web-link"
        try:
            open_link = sys.argv[sys.argv.index(link_flag) + 1].strip()
        except (IndexError, AttributeError):
            print("[!] --open-link requires an http(s) URL")
            sys.exit(1)
        parsed_link = urlparse(open_link)
        if parsed_link.scheme not in ("http", "https") or not parsed_link.netloc:
            print("[!] --open-link requires an http(s) URL")
            sys.exit(1)
    workers = 3
    if "--workers" in sys.argv:
        try:
            workers = int(sys.argv[sys.argv.index("--workers") + 1])
        except (ValueError, IndexError):
            print("[!] --workers must be a positive integer")
            sys.exit(1)
    if workers < 1:
        print("[!] --workers must be a positive integer")
        sys.exit(1)
    if not os.path.exists(accounts_file):
        print("[!] File not found: {}".format(accounts_file))
        sys.exit(1)
    accounts = parse_accounts(accounts_file)
    if not accounts:
        print("[!] No accounts found in file")
        sys.exit(1)
    if codex_web and (workers > 10 or not 1 <= len(accounts) <= 50):
        print("[!] Codex login supports 1-50 accounts and 1-10 concurrent workers")
        sys.exit(1)

    active_workers = min(workers, len(accounts))
    print("=" * 55)
    print("  Auto-Login ChatGPT → 9router (parallel OAuth PKCE)")
    print("=" * 55)
    print("  Accounts: {} | Workers requested: {} | Running: {}".format(len(accounts), workers, active_workers))
    flow = "Codex OAuth only" if codex_web else "ChatGPT Web only" if web_only else "Codex OAuth + 9router import"
    print("  Mode: {} | Flow: {}".format("headed" if headed else "headless", flow))
    print("=" * 55, flush=True)

    results = []
    if web_only:
        _session_shutdown_event.clear()
        threading.Thread(
            target=listen_for_browser_control_commands,
            name="browser-control-listener",
            daemon=True,
        ).start()
        results = run_web_login_queue(accounts, active_workers, slow, open_link, reload_after=reload_after, codex_web=codex_web)
    else:
        start_callback_dispatcher()
        try:
            with ThreadPoolExecutor(max_workers=active_workers, thread_name_prefix="oauth") as executor:
                futures = {
                    executor.submit(login_one_account, index, len(accounts), account, headed, slow): account[0]
                    for index, account in enumerate(accounts, start=1)
                }
                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as error:
                        email = futures[future]
                        emit_event("ERROR", email, error=str(error))
                        results.append({"email": email, "status": "error", "error": str(error)})
        finally:
            stop_callback_dispatcher()

    results.sort(key=lambda result: (result.get("email") or "").lower())
    ok = sum(1 for result in results if result["status"] == "success")
    fail = sum(1 for result in results if result["status"] == "error")
    print("\n{}\n  SUMMARY\n{}\n  ✅ Success: {}\n  ❌ Failed:  {}\n  Total:     {}".format("=" * 55, "=" * 55, ok, fail, len(results)), flush=True)
    out_file = os.path.join(os.path.dirname(os.path.abspath(accounts_file)), "auto_login_results.json")
    with open(out_file, "w", encoding="utf-8") as output:
        json.dump(results, output, indent=2, ensure_ascii=False)
    print("  Results saved: {}".format(out_file), flush=True)

    if web_only:
        with _kept_sessions_lock:
            kept_count = len(_kept_browser_sessions)
        if kept_count:
            print("WEB_READY|{}".format(kept_count), flush=True)
            print("  ChatGPT web sessions are open. Use Stop in the manager to close them.", flush=True)
            try:
                while not _session_shutdown_event.wait(1):
                    with _kept_sessions_lock:
                        if not _kept_browser_sessions:
                            _session_shutdown_event.set()
                            break
            except KeyboardInterrupt:
                _session_shutdown_event.set()


if __name__ == "__main__":
    main()
