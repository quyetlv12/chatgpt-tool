SHOPTAIKHOAN TOOL — macOS APP
=============================

System requirement: Apple Silicon Mac, macOS 13.5 or later.

Install and run:
1. Open Shoptaikhoan-Tool-macOS-arm64.dmg.
2. Drag “Shoptaikhoan Tool” onto the Applications shortcut.
3. Open the app from Applications. The dashboard starts automatically at
   http://127.0.0.1:5033 and opens in the default browser.
4. Use the shield icon on the macOS menu bar to open the tool again or choose
   “Thoát Shoptaikhoan Tool” to stop the server and close the app completely.

The ZIP archive remains available for portable/manual installation.

The app only listens on localhost. Its SQLite database is stored outside the
application at:
  ~/Library/Application Support/InfinityAIStore/Change2FA/twofa.db

This build is ad-hoc signed for local use. If Gatekeeper blocks the first open,
Control-click the app, choose Open, then confirm Open.

Build from source:
  .venv/bin/python -m pip install -r requirements-build.txt
  ./build-macos.sh

The .app, .dmg installer, and ZIP archive are created under dist/.
