#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SOURCE="$ROOT/change 2fa community/static/favicon.svg"
OUTPUT="$ROOT/packaging/macos/AppIcon.icns"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Icon generation requires macOS (qlmanage, sips and iconutil)." >&2
  exit 1
fi

qlmanage -t -s 1024 -o "$WORK" "$SOURCE" >/dev/null 2>&1
PNG="$WORK/$(basename "$SOURCE").png"
ICONSET="$WORK/AppIcon.iconset"
mkdir -p "$ICONSET"

make_size() {
  local pixels="$1"
  local name="$2"
  sips -z "$pixels" "$pixels" "$PNG" --out "$ICONSET/$name" >/dev/null
}

make_size 16 icon_16x16.png
make_size 32 icon_16x16@2x.png
make_size 32 icon_32x32.png
make_size 64 icon_32x32@2x.png
make_size 128 icon_128x128.png
make_size 256 icon_128x128@2x.png
make_size 256 icon_256x256.png
make_size 512 icon_256x256@2x.png
make_size 512 icon_512x512.png
make_size 1024 icon_512x512@2x.png

iconutil -c icns "$ICONSET" -o "$OUTPUT"
echo "Created $OUTPUT"
