#!/data/data/com.termux/files/usr/bin/bash
# Post-receive hook for bare repo auto-deploy on the S21.
# Installed by scripts/setup-git-server.sh (paths substituted at install time).
set -euo pipefail

GIT_DIR="__GIT_DIR__"
WORK_DIR="__WORK_DIR__"
LOG="$WORK_DIR/agent.log"

echo "=== Deploying to $WORK_DIR ==="

GIT_WORK_TREE="$WORK_DIR" git --git-dir="$GIT_DIR" checkout -f main

cd "$WORK_DIR"
pkill -f "palmtop" 2>/dev/null || true
sleep 1

if [ -f .env ]; then
    set -a
    # shellcheck source=/dev/null
    source .env
    set +a
fi

termux-wake-lock 2>/dev/null || true

# Detach from the git-receive-pack / SSH session so `git push` exits promptly.
# Without </dev/null and disown, the agent process can inherit fds 0–2 and hold
# the push connection open after this hook prints "Agent restarted".
nohup .venv/bin/python -m palmtop >>"$LOG" 2>&1 </dev/null &
agent_pid=$!
disown "$agent_pid" 2>/dev/null || disown || true

echo "Agent restarted (PID $agent_pid)"
exit 0
