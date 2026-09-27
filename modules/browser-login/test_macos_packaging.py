import os
import pathlib
import unittest
from unittest import mock

import server


ROOT = pathlib.Path(__file__).resolve().parent


class MacOSPackagingTests(unittest.TestCase):
    def test_packaged_auto_login_binary_is_used_without_python_prefix(self):
        with mock.patch.object(server, "AUTO_LOGIN_EXECUTABLE", "/App/Contents/Resources/bin/auto-login"):
            command = server.build_auto_login_command("/tmp/accounts.txt", 3)

        self.assertEqual(command[0], "/App/Contents/Resources/bin/auto-login")
        self.assertEqual(command[1], "/tmp/accounts.txt")
        self.assertNotIn("auto_login.py", command)

    def test_source_mode_still_uses_python_script(self):
        with mock.patch.object(server, "AUTO_LOGIN_EXECUTABLE", ""):
            command = server.build_auto_login_command("/tmp/accounts.txt", 2)

        self.assertEqual(command[0], server.sys.executable)
        self.assertTrue(command[1].endswith("auto_login.py"))

    def test_macos_bundle_sources_exist(self):
        for relative in (
            "macos/ShopTaiKhoanApp.swift",
            "macos/Info.plist",
            "macos/AppIcon.svg",
            "build_macos.sh",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)


if __name__ == "__main__":
    unittest.main()
