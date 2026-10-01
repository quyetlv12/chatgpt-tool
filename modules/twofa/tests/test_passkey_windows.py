from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "change 2fa community"))
import passkey_windows as windows

URL = "http://127.0.0.1:5033/api/passkey/launch/" + "a" * 43


class PasskeyWindowTests(TestCase):
    def test_close_all_only_terminates_windows_owned_by_launcher(self):
        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid
                self.terminated = False
                self.killed = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                return 0

            def kill(self):
                self.killed = True

        first, second = FakeProcess(11), FakeProcess(12)
        launcher = windows.PasskeyWindows()
        launcher._launches = {
            first.pid: (first, Path("/tmp/passkey-one")),
            second.pid: (second, Path("/tmp/passkey-two")),
        }

        self.assertEqual(launcher.close_all(), 2)
        self.assertTrue(first.terminated)
        self.assertTrue(second.terminated)
        self.assertFalse(first.killed)
        self.assertFalse(second.killed)

    def test_close_all_kills_a_window_when_graceful_close_times_out(self):
        class StubbornProcess:
            pid = 13

            def __init__(self):
                self.terminated = False
                self.killed = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                if not self.killed:
                    raise windows.subprocess.TimeoutExpired("chrome", timeout)
                return -9

            def kill(self):
                self.killed = True

        process = StubbornProcess()
        launcher = windows.PasskeyWindows()
        launcher._launches[process.pid] = (process, Path("/tmp/passkey-stubborn"))

        self.assertEqual(launcher.close_all(), 1)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)

    def test_temporary_profile_defaults_passkey_creation_to_icloud_keychain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Path(temp_dir) / "profile"
            windows.seed_icloud_keychain_preference(profile)

            preferences = profile / "Default" / "Preferences"
            self.assertEqual(
                json.loads(preferences.read_text(encoding="utf-8")),
                {"webauthn": {"create_in_icloud_keychain": True}},
            )
            self.assertEqual(stat.S_IMODE(preferences.stat().st_mode), 0o600)

    def test_tiles_inside_screen_without_overlap(self):
        for width, height in [(1440, 900), (2048, 1152), (1920, 1080)]:
            for count in (1, 2, 3, 4, 6, 10):
                tiles = [windows.calculate_window_bounds(i, count, width, height)
                         for i in range(1, count + 1)]
                for i, tile in enumerate(tiles):
                    self.assertGreaterEqual(tile["left"], 0)
                    self.assertGreaterEqual(tile["top"], 34)
                    self.assertLessEqual(tile["left"] + tile["width"], width)
                    self.assertLessEqual(tile["top"] + tile["height"], height - 74)
                    for other in tiles[i + 1:]:
                        self.assertTrue(tile["left"] + tile["width"] <= other["left"]
                                        or other["left"] + other["width"] <= tile["left"]
                                        or tile["top"] + tile["height"] <= other["top"]
                                        or other["top"] + other["height"] <= tile["top"])

    def test_only_exact_local_handoff_can_be_opened(self):
        windows.validate_local_handoff(URL)
        windows.validate_local_handoff(URL.replace("127.0.0.1", "twofa.localhost"))
        for bad in [URL.replace("127.0.0.1", "evil.test"), URL + "?redirect=evil",
                    URL + "#fragment", URL.replace("/launch/", "/other/"),
                    URL.replace("127.0.0.1", "user@127.0.0.1"), URL + '\"',
                    "https://auth.openai.com/passkey-enroll?mfa_token=secret"]:
            with self.assertRaises(ValueError):
                windows.PasskeyWindows().open(bad, index=1, total=1)

    @patch.object(windows.sys, "platform", "darwin")
    @patch.object(windows, "detect_screen_size", return_value=(1440, 900))
    def test_each_account_starts_an_independent_chrome_profile_and_window(self, _screen):
        fake_process = type("Process", (), {"pid": 101, "wait": lambda self: 0})()
        with patch.object(windows, "find_chrome_executable", return_value=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")), \
             patch.object(windows.tempfile, "mkdtemp", side_effect=["/tmp/passkey-one", "/tmp/passkey-two"]), \
             patch.object(windows, "seed_icloud_keychain_preference") as seed_preference, \
             patch.object(windows.subprocess, "Popen", return_value=fake_process) as launch, \
             patch.object(windows.threading.Thread, "start"):
            launcher = windows.PasskeyWindows()
            self.assertTrue(launcher.open(URL, index=1, total=2))
            self.assertTrue(launcher.open(URL, index=2, total=2))
        self.assertEqual(
            [call.args[0] for call in seed_preference.call_args_list],
            [Path("/tmp/passkey-one"), Path("/tmp/passkey-two")],
        )
        first, second = [call.args[0] for call in launch.call_args_list]
        self.assertIn("--new-window", first)
        self.assertIn("--user-data-dir=/tmp/passkey-one", first)
        self.assertIn("--user-data-dir=/tmp/passkey-two", second)
        self.assertNotEqual(
            next(arg for arg in first if arg.startswith("--window-position=")),
            next(arg for arg in second if arg.startswith("--window-position=")),
        )
        self.assertEqual(first[-1], URL)
        self.assertEqual(second[-1], URL)
        self.assertNotIn("osascript", " ".join(first + second))

    @patch.object(windows.sys, "platform", "darwin")
    def test_chrome_launch_failure_returns_manual_fallback(self):
        with patch.object(windows, "find_chrome_executable", return_value=Path("/missing/chrome")), \
             patch.object(windows.tempfile, "mkdtemp", return_value="/tmp/passkey-failed"), \
             patch.object(windows, "seed_icloud_keychain_preference"), \
             patch.object(windows.subprocess, "Popen", side_effect=OSError("launch denied")), \
             patch.object(windows.shutil, "rmtree") as cleanup:
            self.assertFalse(windows.PasskeyWindows().open(URL, index=1, total=1))
        cleanup.assert_called_once_with(Path("/tmp/passkey-failed"), ignore_errors=True)

    @patch.object(windows.sys, "platform", "darwin")
    def test_profile_preference_failure_returns_manual_fallback(self):
        with patch.object(windows, "find_chrome_executable", return_value=Path("/missing/chrome")), \
             patch.object(windows, "detect_screen_size", return_value=(1440, 900)), \
             patch.object(windows.tempfile, "mkdtemp", return_value="/tmp/passkey-pref-failed"), \
             patch.object(windows, "seed_icloud_keychain_preference", side_effect=OSError("disk full")), \
             patch.object(windows.subprocess, "Popen") as launch, \
             patch.object(windows.shutil, "rmtree") as cleanup:
            self.assertFalse(windows.PasskeyWindows().open(URL, index=1, total=1))
        launch.assert_not_called()
        cleanup.assert_called_once_with(Path("/tmp/passkey-pref-failed"), ignore_errors=True)

    def test_script_and_url_are_stdin_not_process_arguments(self):
        with patch.object(windows.subprocess, "run") as run:
            run.return_value.stdout = "101"
            self.assertEqual(windows._run_script(URL), "101")
        self.assertNotIn(URL, str(run.call_args.args))
        self.assertEqual(run.call_args.kwargs["input"], URL)

    def test_screen_detection_uses_one_display_not_desktop_union(self):
        with patch.dict(os.environ, {"SHOPTAIKHOAN_SCREEN_WIDTH": "0", "SHOPTAIKHOAN_SCREEN_HEIGHT": "0"}), \
             patch.object(windows, "_run_script", return_value="1440,900") as run:
            self.assertEqual(windows.detect_screen_size(), (1440, 900))
        self.assertIn("objectAtIndex(0)", run.call_args.args[0])

    def test_unsupported_platform_does_not_launch_other_browser(self):
        with patch.object(windows.sys, "platform", "linux"), patch.object(windows, "_run_script") as run:
            self.assertFalse(windows.PasskeyWindows().open(URL, index=1, total=1))
            run.assert_not_called()
