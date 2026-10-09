#!/usr/bin/env bash
set -e

# Change directory to the repository root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================================"
echo "  Starting Motive Safety Monitoring Console (Linux)"
echo "  Organizations: HEART (260129) & Houston Towing (599530)"
echo "========================================================"

if [ -f ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
else
    PYTHON_BIN="python"
fi

exec "$PYTHON_BIN" run.py "$@"
