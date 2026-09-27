#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
APP="$ROOT/dist/Shoptaikhoan Tool.app"
ZIP="$ROOT/dist/Shoptaikhoan-Tool-macOS-arm64.zip"
DMG="$ROOT/dist/Shoptaikhoan-Tool-macOS-arm64.dmg"
MENU_BAR_BUILD_DIR="$ROOT/build/macos"
MENU_BAR_HELPER="$MENU_BAR_BUILD_DIR/ShoptaikhoanMenuBar"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "This build currently targets Apple Silicon macOS." >&2
  exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python 3.11 environment not found: $PYTHON_BIN" >&2
  exit 1
fi
if ! "$PYTHON_BIN" -m PyInstaller --version >/dev/null 2>&1; then
  echo "Install build dependencies first:" >&2
  echo "  $PYTHON_BIN -m pip install -r requirements-build.txt" >&2
  exit 1
fi
if ! command -v node >/dev/null 2>&1; then
  echo "Node.js is required so it can be embedded in the application." >&2
  exit 1
fi
if ! xcrun --find swiftc >/dev/null 2>&1; then
  echo "Xcode Command Line Tools with Swift are required." >&2
  exit 1
fi

"$ROOT/packaging/macos/make-icon.sh"
mkdir -p "$MENU_BAR_BUILD_DIR"
xcrun swiftc \
  -parse-as-library \
  -O \
  -target arm64-apple-macos13.5 \
  "$ROOT/packaging/macos/MenuBarApp.swift" \
  -o "$MENU_BAR_HELPER"
cd "$ROOT"
"$PYTHON_BIN" -m PyInstaller --noconfirm --clean ShoptaikhoanTool.spec

# A local ad-hoc signature keeps all nested Mach-O files consistent after
# PyInstaller rewrites their library paths. Distribution can replace this with
# a Developer ID signature and notarization later.
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

# Exercise the frozen import graph and embedded Node runtime under the sparse
# PATH a Finder launch receives.
env PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  "$APP/Contents/MacOS/Shoptaikhoan Tool" --check-runtime-dependencies
"$APP/Contents/Frameworks/node" --version >/dev/null

rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

DMG_STAGE="$(mktemp -d)"
cleanup() {
  rm -rf "$DMG_STAGE"
}
trap cleanup EXIT
ditto "$APP" "$DMG_STAGE/Shoptaikhoan Tool.app"
ln -s /Applications "$DMG_STAGE/Applications"
ditto "$ROOT/MACOS-APP-README.txt" "$DMG_STAGE/HƯỚNG DẪN.txt"
rm -f "$DMG"
hdiutil create \
  -volname "Shoptaikhoan Tool" \
  -srcfolder "$DMG_STAGE" \
  -format UDZO \
  -ov \
  "$DMG"
hdiutil verify "$DMG" >/dev/null

echo "Built: $APP"
echo "Archive: $ZIP"
echo "Installer: $DMG"
