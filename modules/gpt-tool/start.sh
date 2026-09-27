#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN=""
for candidate in python3 python3.13 python3.12 python3.11; do
  if command -v "$candidate" >/dev/null 2>&1 \
    && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "Cần Python 3.11+ . Tải tại https://www.python.org/downloads/"
  exit 1
fi

if [ -x .venv/bin/python ] \
  && ! .venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "Môi trường .venv đang dùng Python cũ, đang tạo lại…"
  rm -rf .venv
fi

if [ ! -x .venv/bin/python ]; then
  echo "Đang tạo môi trường (.venv)…"
  "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
echo "Đang tải / cập nhật thư viện (lần đầu 1–2 phút)…"
.venv/bin/python -m pip install -q -U pip
.venv/bin/python -m pip install -q -U -r requirements.txt
.venv/bin/python -c "from gpt_tool.ensure_deps import ensure_deps; ensure_deps()"
echo "Mở GPT-Tool trên trình duyệt…"
exec .venv/bin/python -m gpt_tool.server
