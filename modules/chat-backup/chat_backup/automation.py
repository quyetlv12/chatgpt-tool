from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

import pyotp
from playwright.sync_api import BrowserContext, Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from .credentials import Credentials
from .storage import BackupStore


CHATGPT = "https://chatgpt.com"
EventCallback = Callable[[str, str], None]


class BackupStopped(RuntimeError):
    pass


class AutomationError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunOptions:
    marker_message: str = "Đã lưu bản sao từ tài khoản nguồn."
    cleanup_new_links: bool = True
    limit: int = 0
    headless: bool = False


_SHARE_URL_RE = re.compile(r"https://(?:chatgpt\.com|chat\.openai\.com)/share/[A-Za-z0-9-]+")
_SHARE_BUTTONS = (
    'button[data-testid="share-chat-button"]',
    '[role="button"][data-testid="share-chat-button"]',
    'button[aria-label*="Share" i]', '[role="button"][aria-label*="Share" i]',
    'button[aria-label*="Chia sẻ" i]', '[role="button"][aria-label*="Chia sẻ" i]',
    'button:has-text("Share")', '[role="button"]:has-text("Share")',
    'button:has-text("Chia sẻ")', '[role="button"]:has-text("Chia sẻ")',
)


def safe_error(error: Exception) -> str:
    message = str(error)
    message = _SHARE_URL_RE.sub("[share-link]", message)
    return message[:1000]


def _check_stop(stop_event: threading.Event) -> None:
    if stop_event.is_set():
        raise BackupStopped("Đã dừng theo yêu cầu")


def _cooldown_seconds(error: Exception | None, failure_streak: int) -> int:
    if error:
        message = safe_error(error).lower()
        if "http 429" in message or "rate limit" in message or "too many requests" in message:
            return random.randint(120, 180)
        return min(120, 30 * max(1, failure_streak) + random.randint(15, 30))
    return random.randint(20, 35)


def _wait_for_cooldown(seconds: int, stop_event: threading.Event, event: EventCallback) -> None:
    for remaining in range(seconds, 0, -1):
        if remaining == seconds or remaining <= 5 or remaining % 5 == 0:
            event("cooldown", f"Đang nghỉ {remaining} giây trước chat tiếp theo")
        if stop_event.wait(1):
            raise BackupStopped("Đã dừng theo yêu cầu")


def _first_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int = 10_000) -> Locator | None:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if locator.is_visible(timeout=250):
                    return locator
            except Exception:
                continue
        page.wait_for_timeout(150)
    return None


def _click(page: Page, selectors: tuple[str, ...], timeout_ms: int = 10_000) -> bool:
    locator = _first_visible(page, selectors, timeout_ms)
    if not locator:
        return False
    locator.click(timeout=5_000)
    return True


def _fill(page: Page, selectors: tuple[str, ...], value: str, timeout_ms: int = 10_000) -> Locator | None:
    locator = _first_visible(page, selectors, timeout_ms)
    if not locator:
        return None
    locator.fill(value, timeout=5_000)
    return locator


def _body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=1_000).lower()
    except Exception:
        return ""


def login(page: Page, credentials: Credentials, stop_event: threading.Event, event: EventCallback) -> None:
    event("login", "Đang mở trang đăng nhập")
    page.goto(f"{CHATGPT}/auth/login", wait_until="domcontentloaded", timeout=60_000)
    _check_stop(stop_event)

    if not _first_visible(page, ('input[type="email"]', 'input[name="email"]', 'input[name="username"]'), 3_000):
        _click(page, (
            'button:has-text("Log in")', 'a:has-text("Log in")',
            'button:has-text("Đăng nhập")', 'a:has-text("Đăng nhập")',
        ), 5_000)

    email = _fill(page, (
        'input[name="email"]', 'input[type="email"]', 'input[name="username"]',
        'input[autocomplete="username"]',
    ), credentials.email, 20_000)
    if not email:
        raise AutomationError("Không tìm thấy ô email")
    if not _click(page, ('button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Tiếp tục")'), 2_000):
        email.press("Enter")

    password = _fill(page, (
        'input[name="password"]', 'input[type="password"]', 'input[autocomplete="current-password"]',
    ), credentials.password, 20_000)
    if not password:
        retry_email = _fill(page, (
            'input[name="email"]', 'input[type="email"]', 'input[name="username"]',
            'input[autocomplete="username"]',
        ), credentials.email, 5_000)
        if retry_email:
            event("email_retry", "Trang email xuất hiện lại, đang gửi lại một lần")
            if not _click(page, (
                'button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Tiếp tục")',
            ), 2_000):
                retry_email.press("Enter")
            password = _fill(page, (
                'input[name="password"]', 'input[type="password"]', 'input[autocomplete="current-password"]',
            ), credentials.password, 30_000)
        if not password:
            raise AutomationError("Không tìm thấy ô mật khẩu sau khi thử lại bước email")
    if not _click(page, ('button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Đăng nhập")'), 2_000):
        password.press("Enter")

    otp = _first_visible(page, (
        'input[name="code"]', 'input[inputmode="numeric"]', 'input[autocomplete="one-time-code"]',
    ), 10_000)
    if otp:
        if not credentials.totp_secret:
            raise AutomationError("Tài khoản yêu cầu 2FA nhưng chưa có secret")
        otp.fill(pyotp.TOTP(credentials.totp_secret).now())
        if not _click(page, ('button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Verify")'), 2_000):
            otp.press("Enter")

    deadline = time.monotonic() + 180
    manual_notice_sent = False
    while time.monotonic() < deadline:
        _check_stop(stop_event)
        current = page.url.lower()
        if current.startswith(f"{CHATGPT}/") and "/auth/" not in current:
            event("authenticated", "Đăng nhập thành công")
            return
        body = _body_text(page)
        if any(text in body for text in ("incorrect password", "wrong password", "invalid code", "mật khẩu không đúng")):
            raise AutomationError("ChatGPT từ chối thông tin đăng nhập hoặc mã 2FA")
        if not manual_notice_sent and any(text in body for text in (
            "verify you are human", "checking your browser", "security check", "phone number required",
        )):
            manual_notice_sent = True
            event("manual", "Cần hoàn tất bước xác minh thủ công trong cửa sổ Chrome")
        page.wait_for_timeout(500)
    raise AutomationError("Hết thời gian chờ đăng nhập")


def fetch_conversations(page: Page, stop_event: threading.Event) -> list[dict[str, str]]:
    conversations: dict[str, dict[str, str]] = {}
    for archived in (False, True):
        offset = 0
        while True:
            _check_stop(stop_event)
            payload = page.evaluate(
                """async ({offset, archived}) => {
                    let session = null;
                    for (let attempt = 0; attempt < 20; attempt += 1) {
                        const response = await fetch('/api/auth/session', {
                            credentials: 'include', cache: 'no-store',
                        });
                        if (response.ok) {
                            session = await response.json();
                            if (typeof session?.accessToken === 'string' && session.accessToken) break;
                        }
                        await new Promise(resolve => setTimeout(resolve, 500));
                    }
                    if (typeof session?.accessToken !== 'string' || !session.accessToken) {
                        throw new Error('authenticated session unavailable');
                    }

                    let accountId = session?.account?.id || session?.accountId || session?.account_id || '';
                    if (!accountId) {
                        try {
                            const encoded = session.accessToken.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
                            const claims = JSON.parse(atob(encoded.padEnd(Math.ceil(encoded.length / 4) * 4, '=')));
                            const auth = claims['https://api.openai.com/auth'] || {};
                            accountId = auth.chatgpt_account_id || auth.account_id || '';
                        } catch (_) {}
                    }

                    const query = new URLSearchParams({
                        offset: String(offset), limit: '100', order: 'updated',
                        is_archived: String(archived), is_starred: 'false',
                    });
                    const path = '/backend-api/conversations';
                    const headers = {
                        'Accept': 'application/json',
                        'Authorization': 'Bearer ' + session.accessToken,
                        'x-openai-target-path': path,
                        'x-openai-target-route': path,
                    };
                    if (accountId) headers['ChatGPT-Account-Id'] = accountId;
                    const response = await fetch(path + '?' + query, {
                        credentials: 'include', cache: 'no-store', headers,
                    });
                    if (!response.ok) throw new Error('HTTP ' + response.status);
                    return await response.json();
                }""",
                {"offset": offset, "archived": archived},
            )
            if isinstance(payload, list):
                items = payload
            elif isinstance(payload, dict):
                items = payload.get("items")
                if items is None:
                    items = payload.get("conversations")
            else:
                items = None
            if not isinstance(items, list):
                raise AutomationError("ChatGPT trả về danh sách cuộc trò chuyện không hợp lệ")
            if not items:
                break
            for item in items:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                conversation_id = str(item["id"])
                conversations[conversation_id] = {
                    "id": conversation_id,
                    "title": str(item.get("title") or "Cuộc trò chuyện không tiêu đề"),
                }
            offset += len(items)
            if len(items) < 100:
                break
    return list(conversations.values())


def _share_url_from_text(value: object) -> str:
    match = _SHARE_URL_RE.search(str(value or ""))
    return match.group(0) if match else ""


def _share_url_from_page(page: Page) -> str:
    try:
        value = page.evaluate(
            r"""() => {
                const values = [...document.querySelectorAll(
                    'input[value], a[href], [data-share-url], [data-url]'
                )].flatMap(node => [
                    node.value || '', node.href || '',
                    node.getAttribute('data-share-url') || '',
                    node.getAttribute('data-url') || '',
                ]);
                const scopes = [...document.querySelectorAll(
                    '[role="dialog"], [role="alertdialog"], [aria-modal="true"], [data-radix-popper-content-wrapper]'
                )];
                for (const scope of scopes) values.push(scope.innerText || '');
                return values.join('\n');
            }"""
        )
    except Exception:
        return ""
    return _share_url_from_text(value)


def _share_url_from_clipboard(page: Page) -> str:
    try:
        value = page.evaluate(
            """async () => {
                if (!navigator.clipboard?.readText) return '';
                try {
                    return await navigator.clipboard.readText();
                } catch (_) {
                    return '';
                }
            }"""
        )
    except Exception:
        return ""
    return _share_url_from_text(value)


def create_share_link(page: Page, conversation_id: str, stop_event: threading.Event) -> tuple[str, bool]:
    page.goto(f"{CHATGPT}/c/{conversation_id}", wait_until="domcontentloaded", timeout=60_000)
    _check_stop(stop_event)
    existing = _share_url_from_page(page)
    if existing:
        return existing, False
    if not _click(page, _SHARE_BUTTONS, 20_000):
        raise AutomationError("Không tìm thấy nút Share")

    deadline = time.monotonic() + 15
    create_clicked = False
    while time.monotonic() < deadline:
        _check_stop(stop_event)
        page_url = _share_url_from_page(page)
        if page_url:
            return page_url, create_clicked
        clipboard_url = _share_url_from_clipboard(page)
        if clipboard_url:
            return clipboard_url, True
        if not create_clicked:
            create_clicked = True
            _click(page, (
                'button:has-text("Create link")', '[role="button"]:has-text("Create link")',
                'button:has-text("Tạo liên kết")', '[role="button"]:has-text("Tạo liên kết")',
                'button:has-text("Copy link")', '[role="button"]:has-text("Copy link")',
                'button:has-text("Sao chép liên kết")', '[role="button"]:has-text("Sao chép liên kết")',
                'button:has-text("Update link")', '[role="button"]:has-text("Update link")',
                'button:has-text("Update and Copy Link")', '[role="button"]:has-text("Update and Copy Link")',
                'button:has-text("Update and copy link")', '[role="button"]:has-text("Update and copy link")',
            ), 2_000)
        page.wait_for_timeout(250)
    raise AutomationError("ChatGPT không trả về link Share")


def copy_shared_conversation(
    page: Page,
    share_url: str,
    message: str,
    stop_event: threading.Event,
) -> str:
    page.goto(share_url, wait_until="domcontentloaded", timeout=60_000)
    _check_stop(stop_event)

    composer_selectors = (
        'textarea[data-testid="prompt-textarea"]', '#prompt-textarea',
        'div[contenteditable="true"][data-testid="prompt-textarea"]', 'div[contenteditable="true"]',
    )
    continue_selectors = (
        'button:has-text("Continue this conversation")', 'a:has-text("Continue this conversation")',
        'button:has-text("Tiếp tục cuộc trò chuyện")', 'a:has-text("Tiếp tục cuộc trò chuyện")',
        'button:has-text("Start new chat")',
    )
    composer = page.locator(", ".join(f"{selector}:visible" for selector in composer_selectors)).first
    continue_button = page.locator(", ".join(f"{selector}:visible" for selector in continue_selectors)).first
    try:
        composer.or_(continue_button).first.wait_for(state="visible", timeout=20_000)
        _check_stop(stop_event)
        if not composer.is_visible():
            continue_button.click(timeout=5_000)
            composer.wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeoutError as exc:
        raise AutomationError("Không tìm thấy ô gửi tin nhắn trên link Share") from exc

    _check_stop(stop_event)
    composer.fill(message)
    if not _click(page, (
        'button[data-testid="send-button"]', 'button[aria-label*="Send" i]',
        'button[aria-label*="Gửi" i]',
    ), 5_000):
        composer.press("Enter")

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        _check_stop(stop_event)
        if re.search(r"https://chatgpt\.com/c/[A-Za-z0-9-]+", page.url):
            return page.url
        page.wait_for_timeout(250)
    raise AutomationError("Đã gửi tin nhắn nhưng chưa xác nhận được chat mới ở tài khoản đích")


def delete_new_share_link(page: Page, conversation_id: str, stop_event: threading.Event) -> bool:
    _check_stop(stop_event)
    conversation_url = f"{CHATGPT}/c/{conversation_id}"
    if page.url.rstrip("/") != conversation_url:
        page.goto(conversation_url, wait_until="domcontentloaded", timeout=60_000)
    _check_stop(stop_event)
    delete_selectors = (
        'button[data-testid="delete-shared-link"]', 'button[aria-label*="Delete link" i]',
        'button[aria-label*="Xóa liên kết" i]', 'button:has-text("Delete link")',
        'button:has-text("Xóa liên kết")',
    )
    # The source page and its Share dialog usually remain open while the target copies.
    delete_button = page.locator(", ".join(f"{selector}:visible" for selector in delete_selectors)).first
    if not delete_button.is_visible() and not _click(page, _SHARE_BUTTONS, 15_000):
        return False
    if not _click(page, delete_selectors, 8_000):
        return False
    _click(page, (
        'button:has-text("Delete")', 'button:has-text("Confirm")',
        'button:has-text("Xóa")', 'button:has-text("Xác nhận")',
    ), 5_000)
    return True


def _new_context(browser) -> BrowserContext:
    return browser.new_context(
        no_viewport=True,
        locale="vi-VN",
        permissions=["clipboard-read", "clipboard-write"],
    )


def run_backup(
    source: Credentials,
    target: Credentials,
    store: BackupStore,
    stop_event: threading.Event,
    event: EventCallback,
    options: RunOptions,
    select_conversations: Callable[[list[dict[str, str]]], list[dict[str, str]]] | None = None,
) -> dict[str, int]:
    counts = {"total": 0, "done": 0, "failed": 0, "skipped": 0, "cleanup_failed": 0}
    failure_streak = 0
    target_authenticated = False
    previous_attempt: tuple[Exception | None, int] | None = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=options.headless,
            args=["--disable-save-password-bubble", "--disable-features=PasswordManagerOnboarding"],
        )
        source_context = _new_context(browser)
        target_context = _new_context(browser)
        source_page = source_context.new_page()
        target_page = target_context.new_page()
        try:
            event("source_login", "Đang đăng nhập tài khoản nguồn")
            login(source_page, source, stop_event, event)
            event("scan", "Đang đọc danh sách cuộc trò chuyện nguồn")
            conversations = fetch_conversations(source_page, stop_event)
            if conversations and select_conversations:
                conversations = select_conversations(conversations)
            _check_stop(stop_event)
            if options.limit > 0:
                conversations = conversations[: options.limit]
            counts["total"] = len(conversations)
            event("total", str(counts["total"]))
            for index, conversation in enumerate(conversations, start=1):
                _check_stop(stop_event)
                conversation_id = conversation["id"]
                title = conversation["title"]
                event("progress", f"{index}/{counts['total']}")
                if store.is_complete(source.email, target.email, conversation_id):
                    counts["skipped"] += 1
                    event("item_skipped", str(counts["skipped"]))
                    continue

                if previous_attempt is not None:
                    _wait_for_cooldown(
                        _cooldown_seconds(*previous_attempt), stop_event, event,
                    )
                if not target_authenticated:
                    event("target_login", "Đang đăng nhập tài khoản đích")
                    login(target_page, target, stop_event, event)
                    target_authenticated = True

                share_url = ""
                created_now = False
                item_error: Exception | None = None
                try:
                    event("sharing", "Đang lấy link Share từ tài khoản nguồn")
                    store.save(source.email, target.email, conversation_id, title, "sharing")
                    share_url, created_now = create_share_link(source_page, conversation_id, stop_event)
                    store.save(
                        source.email, target.email, conversation_id, title, "copying", share_url=share_url,
                    )
                    event("copying", "Đang lưu bản sao vào tài khoản đích")
                    target_url = copy_shared_conversation(
                        target_page, share_url, options.marker_message, stop_event,
                    )
                    cleanup_ok = True
                    if created_now and options.cleanup_new_links:
                        # Checkpoint the copy before optional cleanup so stop/retry cannot duplicate it.
                        store.save(
                            source.email, target.email, conversation_id, title, "complete_cleanup_failed",
                            share_url=share_url, target_url=target_url,
                            error="Đã copy; chưa hoàn tất dọn link Share",
                        )
                        event("cleanup", "Đã lưu bản sao; đang dọn link Share mới")
                        try:
                            cleanup_ok = delete_new_share_link(source_page, conversation_id, stop_event)
                        except BackupStopped:
                            raise
                        except Exception:
                            cleanup_ok = False
                    if not cleanup_ok:
                        counts["cleanup_failed"] += 1
                        event("cleanup_failed", str(counts["cleanup_failed"]))
                    store.save(
                        source.email,
                        target.email,
                        conversation_id,
                        title,
                        "complete" if cleanup_ok else "complete_cleanup_failed",
                        share_url="" if cleanup_ok else share_url,
                        target_url=target_url,
                        error="" if cleanup_ok else "Đã copy nhưng chưa xóa được link Share",
                    )
                    counts["done"] += 1
                    failure_streak = 0
                    event("item_done", str(counts["done"]))
                except BackupStopped:
                    raise
                except Exception as exc:
                    item_error = exc
                    failure_streak += 1
                    counts["failed"] += 1
                    event("item_failed", str(counts["failed"]))
                    store.save(
                        source.email, target.email, conversation_id, title, "failed",
                        share_url=share_url, error=safe_error(exc),
                    )
                    event("item_error", f"{index}/{counts['total']}: {type(exc).__name__}")
                previous_attempt = (item_error, failure_streak)
        finally:
            source_context.close()
            target_context.close()
            browser.close()
    return counts
