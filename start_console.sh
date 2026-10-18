#!/usr/bin/env bash
set -e

# Change directory to the repository root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================================"
echo "  Starting Motive Safety Monitoring Console (Linux)"
echo "  Organizations: HEART (260129) & Houston Towing (599530)"
echo "========================================================"

if [ ! -f ".venv/bin/python" ]; then
    if command -v python3 >/dev/null 2>&1; then
        SYS_PYTHON="python3"
    elif command -v python >/dev/null 2>&1; then
        SYS_PYTHON="python"
    else
        echo "[ERROR] Python 3 is not installed. Please install python3 using your package manager."
        exit 1
    fi
    echo "[*] First-time setup: Creating virtual environment (.venv)..."
    "$SYS_PYTHON" -m venv .venv
fi

PYTHON_BIN=".venv/bin/python"

if ! "$PYTHON_BIN" -c "import fastapi, uvicorn, httpx, playwright, aiofiles" >/dev/null 2>&1; then
    echo "[*] Installing required packages from requirements.txt..."
    "$PYTHON_BIN" -m pip install --upgrade pip
    "$PYTHON_BIN" -m pip install -r requirements.txt
fi

exec "$PYTHON_BIN" run.py "$@"
