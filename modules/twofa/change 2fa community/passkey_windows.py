"""Launch one isolated, operator-owned Chrome window per passkey account."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from pathlib import Path
from urllib.parse import urlsplit


ICLOUD_KEYCHAIN_PREFERENCES = {
    "webauthn": {"create_in_icloud_keychain": True},
}


def calculate_window_bounds(index: int, total: int, width: int, height: int) -> dict[str, int]:
    """Tile Chrome windows in desktop points, with room for the menu and Dock."""
    if not 1 <= index <= total:
        raise ValueError("Invalid passkey window layout")
    width, height = max(320, width), max(240, height)
    gap, top, bottom = 10, 34, 74
    usable_height = height - top - bottom
    columns = min(total, max(1, round(math.sqrt(total * width / usable_height))),
                  max(1, (width - gap) // 610))
    rows = math.ceil(total / columns)
    cell_width = max(1, (width - gap * (columns + 1)) // columns)
    cell_height = max(1, (usable_height - gap * (rows + 1)) // rows)
    return {"left": gap + ((index - 1) % columns) * (cell_width + gap),
            "top": top + gap + ((index - 1) // columns) * (cell_height + gap),
            "width": cell_width, "height": cell_height}


def _run_script(script: str, *, javascript: bool = False) -> str:
    """Run read-only AppKit screen detection; never control another app."""
    command = ["/usr/bin/osascript"]
    if javascript:
        command += ["-l", "JavaScript"]
    result = subprocess.run(command + ["-"], input=script, text=True,
                            capture_output=True, timeout=5, check=True)
    return result.stdout.strip()


def detect_screen_size() -> tuple[int, int]:
    try:
        width = int(os.environ.get("SHOPTAIKHOAN_SCREEN_WIDTH", "0"))
        height = int(os.environ.get("SHOPTAIKHOAN_SCREEN_HEIGHT", "0"))
        if width > 0 and height > 0:
            return width, height
    except ValueError:
        pass
    try:
        output = _run_script('ObjC.import("AppKit"); '
                             'var f=$.NSScreen.screens.objectAtIndex(0).frame; '
                             '[Number(f.size.width),Number(f.size.height)].join(",")',
                             javascript=True)
        width, height = (int(float(value)) for value in output.split(","))
        if width > 0 and height > 0:
            return width, height
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return 1920, 1080


def validate_local_handoff(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1", "twofa.localhost"}
            or parsed.username or parsed.password or not parsed.port
            or parsed.query or parsed.fragment
            or not re.fullmatch(r"/api/passkey/launch/[A-Za-z0-9_-]{43}", parsed.path)
            or any(ord(char) <= 32 for char in url)):
        raise ValueError("Invalid local passkey handoff")


def find_chrome_executable() -> Path | None:
    override = os.environ.get("SHOPTAIKHOAN_CHROME_EXECUTABLE", "").strip()
    candidates = [Path(override)] if override else []
    if sys.platform == "darwin":
        candidates.extend((
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ))
    elif sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(Path(base) / "Google/Chrome/Application/chrome.exe")
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium"):
            resolved = shutil.which(name)
            if resolved:
                candidates.append(Path(resolved))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def seed_icloud_keychain_preference(profile: Path) -> None:
    """Make iCloud Keychain the default WebAuthn provider for this profile.

    The temporary profile is new and contains no account or browser data. This
    writes only Chromium's registered per-profile preference before Chrome
    starts, so a compatible passkey creation request goes straight to the
    native macOS Passwords/Touch ID prompt instead of the provider chooser.
    """
    default_dir = profile / "Default"
    default_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    preferences = default_dir / "Preferences"
    preferences.write_text(
        json.dumps(ICLOUD_KEYCHAIN_PREFERENCES, separators=(",", ":")),
        encoding="utf-8",
    )
    preferences.chmod(0o600)


class PasskeyWindows:
    """Start independent Chrome processes without macOS Automation permission.

    Each account gets a unique temporary Chrome profile, matching the isolated
    browser behavior of ChatGPT Web. Closing the dashboard or queue does not
    close these windows. A watcher removes the temporary profile only after the
    operator closes that Chrome process.
    """
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._launches: dict[int, tuple[subprocess.Popen, Path]] = {}

    def _watch(self, process: subprocess.Popen, profile: Path) -> None:
        try:
            process.wait()
        finally:
            shutil.rmtree(profile, ignore_errors=True)
            with self._lock:
                current = self._launches.get(process.pid)
                if current and current[0] is process:
                    self._launches.pop(process.pid, None)

    def close_all(self, *, timeout: float = 3.0) -> int:
        """Close only Chrome processes created by this launcher instance.

        The launcher never searches for or signals the user's normal Chrome
        processes. A graceful terminate is attempted first; a stubborn
        process is force-killed after the bounded timeout. The watcher still
        owns temporary-profile cleanup when each process exits.
        """
        with self._lock:
            launches = list(self._launches.values())
        deadline = time.monotonic() + max(0.1, float(timeout))
        closed = 0
        for process, _profile in launches:
            try:
                if process.poll() is not None:
                    continue
            except (OSError, AttributeError):
                continue
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            closed += 1
        return closed

    def open(self, url: str, *, index: int, total: int) -> bool:
        validate_local_handoff(url)
        chrome = find_chrome_executable()
        if chrome is None:
            return False
        width, height = detect_screen_size()
        bounds = calculate_window_bounds(index, total, width, height)
        profile = Path(tempfile.mkdtemp(prefix="shoptaikhoan-passkey-chrome-"))
        try:
            seed_icloud_keychain_preference(profile)
        except OSError:
            shutil.rmtree(profile, ignore_errors=True)
            return False
        command = [
            str(chrome),
            "--new-window",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--disable-session-crashed-bubble",
            "--disable-save-password-bubble",
            "--disable-features=PasswordManagerOnboarding,PasswordLeakDetection",
            f"--user-data-dir={profile}",
            f"--window-position={bounds['left']},{bounds['top']}",
            f"--window-size={bounds['width']},{bounds['height']}",
            url,
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            shutil.rmtree(profile, ignore_errors=True)
            return False
        with self._lock:
            self._launches[process.pid] = (process, profile)
        threading.Thread(
            target=self._watch,
            args=(process, profile),
            name=f"passkey-chrome-{process.pid}",
            daemon=True,
        ).start()
        return True
