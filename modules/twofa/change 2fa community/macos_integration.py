"""Native macOS menu-bar companion launcher for the frozen application."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


MENU_BAR_HELPER = "ShoptaikhoanMenuBar"


def menu_bar_command(
    host: str,
    port: int,
    parent_pid: int | None = None,
    bundle_root: Path | None = None,
) -> list[str] | None:
    """Build the native helper command when its bundled executable is present."""
    root = bundle_root or Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    helper = root / MENU_BAR_HELPER
    if not helper.is_file() or not os.access(helper, os.X_OK):
        return None
    url_host = "127.0.0.1" if host in {"localhost", "::1"} else host
    return [
        str(helper),
        "--parent-pid",
        str(parent_pid or os.getpid()),
        "--url",
        f"http://{url_host}:{port}/",
    ]


def launch_menu_bar(
    host: str,
    port: int,
    *,
    platform: str | None = None,
    frozen: bool | None = None,
    parent_pid: int | None = None,
    bundle_root: Path | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> Any | None:
    """Launch the companion only for the Finder-distributed macOS build."""
    current_platform = sys.platform if platform is None else platform
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if current_platform != "darwin" or not is_frozen:
        return None
    command = menu_bar_command(host, port, parent_pid, bundle_root)
    if not command:
        return None
    try:
        return popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        # The web server remains usable even if macOS refuses to start the
        # optional native menu companion.
        return None
