#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/.venv"

echo "============================================"
echo "  Shoptaikhoan Tool - Change 2FA"
echo "  Source Edition"
echo "============================================"
echo

# --- Check Python ---
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON="$cmd"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "[ERROR] Python khong duoc tim thay."
    echo "Cai Python 3.11+:"
    echo "  Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip"
    echo "  macOS:         brew install python"
    exit 1
fi

# --- Create venv if missing ---
if [ ! -f "$VENV/bin/python" ]; then
    echo "[1/3] Dang tao moi truong ao..."
    "$PYTHON" -m venv "$VENV"
fi

# --- Install deps ---
echo "[2/3] Dang cai dat dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$ROOT/requirements-source.txt"

# --- Env ---
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# --- XDG data dir ---
if [ "$(uname)" = "Darwin" ]; then
    DATA_DIR="$HOME/Library/Application Support/InfinityAIStore/Change2FA"
else
    DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/InfinityAIStore/Change2FA"
fi
mkdir -p "$DATA_DIR"

# --- Start server ---
echo "[3/3] Dang khoi dong server..."
echo
echo "  URL: http://127.0.0.1:5033"
echo "  Nhan Ctrl+C de dung server."
echo
"$VENV/bin/python" "$ROOT/change 2fa community/server.py" --host 127.0.0.1 --port 5033
