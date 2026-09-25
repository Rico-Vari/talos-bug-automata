#!/usr/bin/env bash
# Foreground alternative to the systemd units: runs the Telegram bot and the
# dispatch watcher side by side until Ctrl+C.
set -euo pipefail
cd "$(dirname "$0")/.."
source venv/bin/activate

echo "Starting Telegram bot..."
talos-bot &
BOT_PID=$!

echo "Starting dispatch watcher..."
talos-dispatch --watch &
DISPATCH_PID=$!

echo "Bot PID: $BOT_PID | Dispatch PID: $DISPATCH_PID"
echo "Ctrl+C stops both."

trap "kill $BOT_PID $DISPATCH_PID 2>/dev/null; exit" INT TERM
wait
