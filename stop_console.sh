#!/usr/bin/env bash

# Stop Motive Safety Console server on Linux
PORT="${1:-8000}"

echo "Stopping Motive Safety Console on port ${PORT}..."

# 1. Attempt graceful shutdown via API endpoint
if command -v curl >/dev/null 2>&1; then
    RESPONSE=$(curl -s -m 2 -X POST "http://127.0.0.1:${PORT}/api/stop" 2>/dev/null)
    if [ -n "$RESPONSE" ]; then
        echo "[+] Sent shutdown request to API: $RESPONSE"
        sleep 1
    fi
fi

# 2. Check if anything is still listening on the port
PIDS=$(lsof -t -iTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null || fuser "${PORT}/tcp" 2>/dev/null || true)

if [ -n "$PIDS" ]; then
    echo "[*] Killing remaining process(es) on port ${PORT}: ${PIDS}"
    for pid in $PIDS; do
        kill "$pid" 2>/dev/null || true
    done
    sleep 0.5
    # Force kill if still lingering
    for pid in $PIDS; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    echo "[+] Motive Safety Console stopped successfully."
else
    echo "[+] No active process found listening on port ${PORT}."
fi
