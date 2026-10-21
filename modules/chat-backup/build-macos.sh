#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" -c 'import PyInstaller, playwright, pyotp'
rm -rf "$ROOT_DIR/build" "$ROOT_DIR/dist"
"$PYTHON_BIN" -m PyInstaller --noconfirm --clean --onedir --contents-directory chat-backup-runtime \
  --name chat-backup-server \
  --paths "$ROOT_DIR" \
  --add-data "$ROOT_DIR/web:web" \
  --collect-all playwright \
  --hidden-import pyotp \
  --distpath "$ROOT_DIR/dist" \
  --workpath "$ROOT_DIR/build/work" \
  --specpath "$ROOT_DIR/build/spec" \
  "$ROOT_DIR/run.py"
