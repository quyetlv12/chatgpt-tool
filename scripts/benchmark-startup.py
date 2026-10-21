"""Measure packaged startup with empty temporary data, without opening a browser or creating jobs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen


def help_time(executable: Path) -> float:
    started = time.perf_counter()
    subprocess.run([str(executable), "--help"], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, timeout=20)
    return round(time.perf_counter() - started, 3)


def suite_time(resources: Path) -> dict:
    engines = resources / "engines"
    with tempfile.TemporaryDirectory(prefix="stb-", dir="/tmp") as directory, tempfile.TemporaryFile() as log:
        runtime = Path(directory)
        env = {**os.environ,
            "SHOPTAIKHOAN_SUITE_DATA_DIR": str(runtime),
            "SHOPTAIKHOAN_SUITE_TWOFA_EXECUTABLE": str(engines / "TwoFA.app/Contents/MacOS/Shoptaikhoan Tool"),
            "SHOPTAIKHOAN_SUITE_EXPORT_EXECUTABLE": str(engines / "GPT-Tool.app/Contents/MacOS/GPT-Tool"),
            "SHOPTAIKHOAN_SUITE_BROWSER_EXECUTABLE": str(engines / "BrowserLogin.app/Contents/Resources/bin/shoptaikhoan-server"),
            "SHOPTAIKHOAN_SUITE_CHAT_BACKUP_EXECUTABLE": str(engines / "chat-backup-server"),
            "SHOPTAIKHOAN_AUTO_LOGIN_EXECUTABLE": str(engines / "BrowserLogin.app/Contents/Resources/bin/shoptaikhoan-auto-login"),
            "PLAYWRIGHT_BROWSERS_PATH": str(engines / "BrowserLogin.app/Contents/Resources/ms-playwright"),
            "SHOPTAIKHOAN_NO_MENU_BAR": "1",
            "PYTHONUNBUFFERED": "1",
        }
        started = time.perf_counter()
        process = subprocess.Popen(
            [str(resources / "bin/shoptaikhoan-suite-hub"), "--port", "0", "--no-browser"],
            cwd=resources, env=env, stdout=log, stderr=log, start_new_session=True,
        )
        result = {"hub_s": None, "modules_s": {}}
        url = ""
        try:
            deadline = started + 45
            while time.perf_counter() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Temporary Suite exited before all modules became ready")
                if not url:
                    log.seek(0)
                    match = re.search(rb"Shoptaikhoan Suite .*?(http://127\.0\.0\.1:[0-9]+/)", log.read())
                    if match:
                        url = match.group(1).decode()
                if url:
                    try:
                        with urlopen(url + "api/status", timeout=1) as response:
                            state = json.load(response)
                    except (OSError, ValueError):
                        time.sleep(0.1)
                        continue
                    elapsed = round(time.perf_counter() - started, 3)
                    if result["hub_s"] is None:
                        result["hub_s"] = elapsed
                    for module in state["modules"]:
                        if module["ready"]:
                            result["modules_s"].setdefault(module["key"], elapsed)
                    if set(result["modules_s"]) == {"twofa", "export", "browser", "chat-backup"}:
                        result["all_ready_s"] = elapsed
                        assert state["browser_windows_open"] is False
                        for host in ("twofa", "export", "browser", "backup"):
                            for path in ("", "api/health"):
                                request = Request(url + path, headers={"Host": f"{host}.localhost"})
                                with urlopen(request, timeout=3) as response:
                                    assert response.status == 200
                                    response.read()
                        request = Request(url + "api/status", headers={"Host": "backup.localhost"})
                        with urlopen(request, timeout=3) as response:
                            backup = json.load(response)
                            assert backup["running"] is False and backup["stage"] == "idle"
                        with urlopen(Request(url + "api/shutdown", method="POST"), timeout=3) as response:
                            assert json.load(response) == {"ok": True}
                        process.wait(timeout=20)
                        assert not list((runtime / "s").glob("*.sock")), "Temporary module sockets were not closed"
                        return result
                time.sleep(0.1)
            raise TimeoutError("Temporary Suite startup exceeded 45 seconds")
        finally:
            if process.poll() is None:
                # This process group belongs only to this isolated benchmark run.
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--suite", action="store_true", help="Also start and shut down all four modules on isolated empty data")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 5:
        parser.error("repeats must be between 1 and 5")
    resources = args.app.resolve() / "Contents/Resources"
    result = {"app": str(args.app), "scope": "Warm-cache startup, no jobs or account operations", "help": {}}
    for name, executable in (
        ("hub", resources / "bin/shoptaikhoan-suite-hub"),
        ("chat-backup", resources / "engines/chat-backup-server"),
    ):
        samples = [help_time(executable) for _ in range(args.repeats)]
        result["help"][name] = {"samples_s": samples, "median_s": statistics.median(samples)}
    if args.suite:
        samples = [suite_time(resources) for _ in range(args.repeats)]
        result["suite"] = {"samples": samples, "median_all_ready_s": statistics.median(sample["all_ready_s"] for sample in samples)}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
