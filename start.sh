#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Load secrets
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# Prevent Android from killing Termux
termux-wake-lock

# Kill any existing instance
pkill -f "pocket_agent" 2>/dev/null || true
sleep 1

# Start the agent
nohup .venv/bin/python -m pocket_agent > agent.log 2>&1 &
echo "Agent started (PID $!). Logs: tail -f agent.log"
