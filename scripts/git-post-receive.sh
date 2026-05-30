#!/data/data/com.termux/files/usr/bin/bash
# Post-receive hook for bare repo auto-deploy on the S21.
# Installed by scripts/setup-git-server.sh (paths substituted at install time).
#
# SECURITY: this hook checks out and RUNS whatever is pushed. Anyone who can
# push to this repo therefore gets remote code execution as the agent user.
# There is no auth in the hook itself — it relies entirely on the SSH server:
#   - Use key-only SSH auth (disable password login).
#   - Bind sshd to the Tailscale interface; never expose the git port publicly.
#   - Restrict who may log in (AllowUsers / a dedicated deploy key).
# Optionally set PALMTOP_REQUIRE_SIGNED=1 (in the hook's environment) to refuse
# any pushed commit that isn't signed by a key in your allowed signers.
set -euo pipefail

GIT_DIR="__GIT_DIR__"
WORK_DIR="__WORK_DIR__"
LOG="$WORK_DIR/agent.log"

echo "=== Deploying to $WORK_DIR ==="

# Optional: require signed commits before deploying (off by default).
if [ "${PALMTOP_REQUIRE_SIGNED:-0}" = "1" ]; then
    while read -r _old newrev _ref; do
        [ "$newrev" = "0000000000000000000000000000000000000000" ] && continue
        if ! git --git-dir="$GIT_DIR" verify-commit "$newrev" >/dev/null 2>&1; then
            echo "SECURITY: refusing deploy — commit $newrev is not signed by a trusted key" >&2
            exit 1
        fi
    done
fi

GIT_WORK_TREE="$WORK_DIR" git --git-dir="$GIT_DIR" checkout -f main

cd "$WORK_DIR"
pkill -f "palmtop" 2>/dev/null || true
sleep 1

# Loads the LOCAL .env (gitignored, so it is not part of the pushed tree and
# survives `checkout -f`). Keep secrets here, never committed to the repo.
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
