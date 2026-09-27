#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -m pip install -q -U -r requirements.txt -r requirements-build.txt
"$PYTHON_BIN" -m PyInstaller --noconfirm --clean gpt-tool.spec

test -d dist/GPT-Tool.app
rm -f dist/GPT-Tool-macOS.zip
ditto -c -k --sequesterRsrc --keepParent dist/GPT-Tool.app dist/GPT-Tool-macOS.zip
echo "Built: $ROOT/dist/GPT-Tool.app"
echo "Archive: $ROOT/dist/GPT-Tool-macOS.zip"
