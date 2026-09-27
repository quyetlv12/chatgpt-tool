#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$ROOT_DIR/build/macos"
DIST_DIR="$ROOT_DIR/dist"
APP_NAME="shoptaikhoan auto tool"
APP_BUNDLE="$DIST_DIR/$APP_NAME.app"
DMG_PATH="$DIST_DIR/$APP_NAME-macOS-arm64.dmg"
PYTHON_BIN="${PYTHON_BIN:-python3}"

safe_clean() {
  local target="$1"
  case "$target" in
    "$ROOT_DIR/build/macos"|"$ROOT_DIR/dist") rm -rf "$target" ;;
    *) echo "Refusing to clean unexpected path: $target" >&2; exit 1 ;;
  esac
}

echo "[1/8] Checking build dependencies"
"$PYTHON_BIN" -c "import PyInstaller, playwright, pyotp"
command -v swiftc >/dev/null
command -v hdiutil >/dev/null
command -v codesign >/dev/null
command -v iconutil >/dev/null
command -v sips >/dev/null

safe_clean "$BUILD_DIR"
safe_clean "$DIST_DIR"
mkdir -p "$BUILD_DIR/py-dist" "$BUILD_DIR/py-work" "$BUILD_DIR/specs" "$DIST_DIR"

echo "[2/8] Building local server binary"
"$PYTHON_BIN" -m PyInstaller \
  --noconfirm --clean --onefile \
  --name shoptaikhoan-server \
  --add-data "$ROOT_DIR/index.html:." \
  --distpath "$BUILD_DIR/py-dist" \
  --workpath "$BUILD_DIR/py-work/server" \
  --specpath "$BUILD_DIR/specs" \
  "$ROOT_DIR/server.py"

echo "[3/8] Building Playwright automation binary"
"$PYTHON_BIN" -m PyInstaller \
  --noconfirm --clean --onefile \
  --name shoptaikhoan-auto-login \
  --collect-all playwright \
  --hidden-import pyotp \
  --distpath "$BUILD_DIR/py-dist" \
  --workpath "$BUILD_DIR/py-work/auto-login" \
  --specpath "$BUILD_DIR/specs" \
  "$ROOT_DIR/auto_login.py"

echo "[4/8] Assembling macOS application bundle"
mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources/bin"
cp "$ROOT_DIR/macos/Info.plist" "$APP_BUNDLE/Contents/Info.plist"
cp "$BUILD_DIR/py-dist/shoptaikhoan-server" "$APP_BUNDLE/Contents/Resources/bin/"
cp "$BUILD_DIR/py-dist/shoptaikhoan-auto-login" "$APP_BUNDLE/Contents/Resources/bin/"
chmod 755 "$APP_BUNDLE/Contents/Resources/bin/"*

swiftc \
  -O \
  -target arm64-apple-macos12.0 \
  -framework AppKit \
  -framework Foundation \
  "$ROOT_DIR/macos/ShopTaiKhoanApp.swift" \
  -o "$APP_BUNDLE/Contents/MacOS/ShopTaiKhoanAutoTool"

echo "[5/8] Creating application icon"
ICONSET="$BUILD_DIR/AppIcon.iconset"
SOURCE_PNG="$BUILD_DIR/AppIcon-1024.png"
mkdir -p "$ICONSET"
sips -s format png "$ROOT_DIR/macos/AppIcon.svg" --out "$SOURCE_PNG" >/dev/null
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$SOURCE_PNG" --out "$ICONSET/icon_"$size"x"$size".png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$SOURCE_PNG" --out "$ICONSET/icon_"$size"x"$size"@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP_BUNDLE/Contents/Resources/AppIcon.icns"

echo "[6/8] Bundling the matching Playwright Chromium runtime"
CHROMIUM_DIR="$("$PYTHON_BIN" - <<'PY'
from pathlib import Path
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    executable = Path(playwright.chromium.executable_path)
for parent in executable.parents:
    if parent.name.startswith("chromium-"):
        print(parent)
        break
else:
    raise SystemExit("Cannot locate Playwright Chromium cache")
PY
)"
PLAYWRIGHT_CACHE="$(dirname "$CHROMIUM_DIR")"
CHROMIUM_REVISION="$(basename "$CHROMIUM_DIR" | sed 's/^chromium-//')"
mkdir -p "$APP_BUNDLE/Contents/Resources/ms-playwright"
ditto "$CHROMIUM_DIR" "$APP_BUNDLE/Contents/Resources/ms-playwright/$(basename "$CHROMIUM_DIR")"
if [ -d "$PLAYWRIGHT_CACHE/chromium_headless_shell-$CHROMIUM_REVISION" ]; then
  ditto "$PLAYWRIGHT_CACHE/chromium_headless_shell-$CHROMIUM_REVISION" "$APP_BUNDLE/Contents/Resources/ms-playwright/chromium_headless_shell-$CHROMIUM_REVISION"
fi
if [ -d "$PLAYWRIGHT_CACHE/ffmpeg-1011" ]; then
  ditto "$PLAYWRIGHT_CACHE/ffmpeg-1011" "$APP_BUNDLE/Contents/Resources/ms-playwright/ffmpeg-1011"
fi

echo "[7/8] Ad-hoc signing application"
codesign --force --deep --sign - --timestamp=none "$APP_BUNDLE"
codesign --verify --deep --strict --verbose=2 "$APP_BUNDLE"

echo "[8/8] Creating drag-to-install DMG"
DMG_STAGE="$BUILD_DIR/dmg"
mkdir -p "$DMG_STAGE"
ditto "$APP_BUNDLE" "$DMG_STAGE/$APP_NAME.app"
ln -s /Applications "$DMG_STAGE/Applications"
hdiutil create \
  -volname "$APP_NAME" \
  -srcfolder "$DMG_STAGE" \
  -ov -format UDZO \
  "$DMG_PATH"

echo
echo "Build complete:"
echo "  App: $APP_BUNDLE"
echo "  DMG: $DMG_PATH"
