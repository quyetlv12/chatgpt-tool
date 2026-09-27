from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "change 2fa community"
for path in (str(ROOT), str(APP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from macos_integration import launch_menu_bar, menu_bar_command  # noqa: E402


class MacOSIntegrationTests(unittest.TestCase):
    def test_packaging_declares_native_menu_and_dmg_installer(self) -> None:
        swift_source = (ROOT / "packaging/macos/MenuBarApp.swift").read_text()
        spec_source = (ROOT / "ShoptaikhoanTool.spec").read_text()
        build_source = (ROOT / "build-macos.sh").read_text()

        self.assertIn('title: "Mở Tool"', swift_source)
        self.assertIn('title: "Thoát Shoptaikhoan Tool"', swift_source)
        self.assertIn("Darwin.kill(parentPID, SIGTERM)", swift_source)
        self.assertIn('"LSUIElement": True', spec_source)
        self.assertIn("ShoptaikhoanMenuBar", spec_source)
        self.assertIn("hdiutil create", build_source)
        self.assertIn("Applications", build_source)

    def test_builds_loopback_menu_bar_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "ShoptaikhoanMenuBar"
            helper.touch(mode=0o755)

            command = menu_bar_command(
                host="::1",
                port=5033,
                parent_pid=1234,
                bundle_root=Path(directory),
            )

        self.assertEqual(
            command,
            [
                str(helper),
                "--parent-pid",
                "1234",
                "--url",
                "http://127.0.0.1:5033/",
            ],
        )

    def test_launches_helper_only_for_frozen_macos_app(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "ShoptaikhoanMenuBar"
            helper.touch(mode=0o755)
            calls: list[tuple[list[str], dict]] = []

            def fake_popen(command: list[str], **kwargs):
                calls.append((command, kwargs))
                return object()

            process = launch_menu_bar(
                host="localhost",
                port=5033,
                platform="darwin",
                frozen=True,
                parent_pid=5678,
                bundle_root=Path(directory),
                popen=fake_popen,
            )

        self.assertIsNotNone(process)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][1:], [
            "--parent-pid",
            "5678",
            "--url",
            "http://127.0.0.1:5033/",
        ])
        self.assertTrue(calls[0][1]["start_new_session"])

    def test_skips_helper_outside_frozen_macos_app(self) -> None:
        def fail_popen(*_args, **_kwargs):
            raise AssertionError("menu helper must not launch")

        self.assertIsNone(launch_menu_bar(
            host="127.0.0.1",
            port=5033,
            platform="linux",
            frozen=True,
            popen=fail_popen,
        ))
        self.assertIsNone(launch_menu_bar(
            host="127.0.0.1",
            port=5033,
            platform="darwin",
            frozen=False,
            popen=fail_popen,
        ))

    def test_missing_helper_does_not_block_server_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(launch_menu_bar(
                host="127.0.0.1",
                port=5033,
                platform="darwin",
                frozen=True,
                bundle_root=Path(directory),
            ))


if __name__ == "__main__":
    unittest.main()
