"""Startup optimization is packaging-only; executable paths and readiness gates stay stable."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StartupPackagingTests(unittest.TestCase):
    def test_hub_and_backup_keep_runtime_inside_bundle_without_onefile_extraction(self):
        for relative, runtime in (
            ("build-macos.sh", "hub-runtime"),
            ("modules/chat-backup/build-macos.sh", "chat-backup-runtime"),
        ):
            with self.subTest(relative=relative):
                script = (ROOT / relative).read_text()
                self.assertIn(f"--onedir --contents-directory {runtime}", script)
                self.assertNotIn("--onefile", script)

    def test_build_copies_complete_runtime_folders_to_existing_executable_locations(self):
        script = (ROOT / "build-macos.sh").read_text()
        self.assertIn('ditto "$BUILD/hub-dist/shoptaikhoan-suite-hub" "$APP/Contents/Resources/bin"', script)
        self.assertIn('ditto "$ROOT/modules/chat-backup/dist/chat-backup-server" "$APP/Contents/Resources/engines"', script)
        self.assertIn('! -d "$ROOT/modules/chat-backup/dist/chat-backup-server/chat-backup-runtime"', script)
        self.assertLess(script.index("Chat Backup engine cũ"), script.index('rm -rf "$BUILD" "$DIST"'))

    def test_launcher_paths_and_all_modules_ready_gate_are_unchanged(self):
        launcher = (ROOT / "packaging/macos/ShoptaikhoanSuite.swift").read_text()
        self.assertIn('subdirectory: "bin"', launcher)
        self.assertIn('engines.appendingPathComponent("chat-backup-server").path', launcher)
        self.assertIn('process.arguments = ["--no-browser"]', launcher)
        self.assertIn('completion(modules.allSatisfy { $0["ready"] as? Bool == true })', launcher)


if __name__ == "__main__":
    unittest.main()
