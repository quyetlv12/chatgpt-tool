#!/bin/zsh

set -u

TOOL_DIR="$(cd "$(dirname "$0")" && pwd)"
TOOL_URL="http://localhost:9876/"

open_tool_ui() {
  if [[ "${SHOPTAIKHOAN_LAUNCHER_NO_OPEN:-0}" != "1" ]]; then
    open "$TOOL_URL"
  fi
}

echo
echo "  shoptaikhoan auto tool"
echo "  ======================="
echo

if curl --silent --fail --max-time 2 "$TOOL_URL/api/health" >/dev/null 2>&1; then
  echo "  Source server dang chay. Dang mo lai giao dien..."
  open_tool_ui
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "  [!] Khong tim thay Python 3."
  echo "  Hay cai Python 3 de chay truc tiep ma nguon."
  echo
  read -r "?Nhan Enter de dong..."
  exit 1
fi

cd "$TOOL_DIR" || exit 1

echo "  Dang kiem tra thu vien..."
if ! python3 -c "import pyotp, playwright" >/dev/null 2>&1; then
  echo "  Dang cai pyotp va Playwright (chi can o lan dau)..."
  python3 -m pip install --user --upgrade pyotp playwright || {
    echo "  [!] Khong the cai thu vien Python."
    read -r "?Nhan Enter de dong..."
    exit 1
  }
fi

if ! python3 - <<'PY' >/dev/null 2>&1
from pathlib import Path
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    executable = Path(playwright.chromium.executable_path)

raise SystemExit(0 if executable.exists() else 1)
PY
then
  echo "  Dang tai Chromium cho Playwright (chi can o lan dau)..."
  python3 -m playwright install chromium || {
    echo "  [!] Khong the cai Chromium."
    read -r "?Nhan Enter de dong..."
    exit 1
  }
fi

echo "  Dang chay truc tiep: python3 server.py"
echo "  Giao dien: $TOOL_URL"
echo "  Giu cua so nay mo trong khi su dung. Nhan Ctrl+C de dung."
echo
exec python3 "$TOOL_DIR/server.py"
