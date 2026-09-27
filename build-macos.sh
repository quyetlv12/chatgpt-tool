#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
BUILD="$ROOT/build/macos"
DIST="$ROOT/dist"
APP="$DIST/Shoptaikhoan Suite.app"

[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || { echo "Build này dành cho Apple Silicon macOS." >&2; exit 1; }
"$PYTHON_BIN" -c 'import PyInstaller, playwright, curl_cffi, fastapi'
command -v xcrun >/dev/null

rm -rf "$BUILD" "$DIST"
mkdir -p "$BUILD" "$DIST" "$APP/Contents/MacOS" "$APP/Contents/Resources/bin" "$APP/Contents/Resources/engines"

echo "[1/5] Building module engines"
if [[ "${REUSE_ENGINES:-0}" != "1" ]]; then
  PYTHON_BIN="$PYTHON_BIN" "$ROOT/modules/twofa/build-macos.sh"
  (cd "$ROOT/modules/gpt-tool" && "$PYTHON_BIN" -m PyInstaller --noconfirm --clean gpt-tool.spec)
  PYTHON_BIN="$PYTHON_BIN" "$ROOT/modules/browser-login/build_macos.sh"
fi

echo "[2/5] Building Suite hub"
"$PYTHON_BIN" -m PyInstaller --noconfirm --clean --onefile \
  --name shoptaikhoan-suite-hub \
  --add-data "$ROOT/web:web" \
  --distpath "$BUILD/hub-dist" --workpath "$BUILD/hub-work" --specpath "$BUILD/hub-spec" \
  "$ROOT/suite.py"
cp "$BUILD/hub-dist/shoptaikhoan-suite-hub" "$APP/Contents/Resources/bin/"

echo "[3/5] Assembling one application"
ditto "$ROOT/modules/twofa/dist/Shoptaikhoan Tool.app" "$APP/Contents/Resources/engines/TwoFA.app"
ditto "$ROOT/modules/gpt-tool/dist/GPT-Tool.app" "$APP/Contents/Resources/engines/GPT-Tool.app"
ditto "$ROOT/modules/browser-login/dist/shoptaikhoan auto tool.app" "$APP/Contents/Resources/engines/BrowserLogin.app"
cp "$ROOT/packaging/macos/Info.plist" "$APP/Contents/Info.plist"
ICON_WORK="$BUILD/icon"
mkdir -p "$ICON_WORK/AppIcon.iconset"
qlmanage -t -s 1024 -o "$ICON_WORK" "$ROOT/web/favicon.svg" >/dev/null 2>&1
ICON_PNG="$ICON_WORK/favicon.svg.png"
for spec in "16 icon_16x16.png" "32 icon_16x16@2x.png" "32 icon_32x32.png" "64 icon_32x32@2x.png" "128 icon_128x128.png" "256 icon_128x128@2x.png" "256 icon_256x256.png" "512 icon_256x256@2x.png" "512 icon_512x512.png" "1024 icon_512x512@2x.png"; do
  set -- $spec; sips -z "$1" "$1" "$ICON_PNG" --out "$ICON_WORK/AppIcon.iconset/$2" >/dev/null
done
iconutil -c icns "$ICON_WORK/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
cp "$ROOT/packaging/macos/MenuBarTemplate.svg" "$APP/Contents/Resources/MenuBarTemplate.svg"
xcrun swiftc -O -target arm64-apple-macos13.5 \
  "$ROOT/packaging/macos/ShoptaikhoanSuite.swift" -o "$APP/Contents/MacOS/ShoptaikhoanSuite"

echo "[4/5] Signing"
codesign --force --deep --sign - --timestamp=none "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "[5/5] Creating ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$DIST/Shoptaikhoan-Suite-macOS-arm64.zip"
echo "Built: $APP"
