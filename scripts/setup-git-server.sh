#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

# Set up S21 as a git server for palmtop.
# Run this on the S21 via Termux.
#
# After setup, add the remote on your Mac:
#   git remote add s21 ssh://user@<s21-tailscale-ip>:8022/~/git/s21-agent.git
#   git push s21 main
#
# The post-receive hook auto-deploys: pulls into the working dir and restarts.

GIT_DIR="$HOME/git/s21-agent.git"
WORK_DIR="$HOME/projects/s21-agent"

echo "=== Setting up git server ==="

# Ensure git is installed
pkg install -y git 2>/dev/null || true

# Create bare repo
if [ -d "$GIT_DIR" ]; then
    echo "Bare repo already exists at $GIT_DIR"
else
    mkdir -p "$HOME/git"
    git init --bare "$GIT_DIR"
    echo "Created bare repo at $GIT_DIR"
fi

# Install post-receive hook (see scripts/git-post-receive.sh)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOOK_SRC="$SCRIPT_DIR/git-post-receive.sh"
if [ ! -f "$HOOK_SRC" ]; then
    echo "Missing hook template: $HOOK_SRC" >&2
    exit 1
fi
sed "s|__GIT_DIR__|$GIT_DIR|g; s|__WORK_DIR__|$WORK_DIR|g" "$HOOK_SRC" > "$GIT_DIR/hooks/post-receive"
chmod +x "$GIT_DIR/hooks/post-receive"

# Point the working directory at the bare repo
cd "$WORK_DIR"
if git remote get-url origin 2>/dev/null | grep -q "github.com"; then
    git remote rename origin github 2>/dev/null || true
    echo "Renamed existing github remote to 'github'"
fi

if git remote get-url origin 2>/dev/null; then
    git remote set-url origin "$GIT_DIR"
else
    git remote add origin "$GIT_DIR"
fi

echo ""
echo "=== Done ==="
echo ""
echo "On your Mac, add the S21 remote:"
echo "  git remote add s21 ssh://\$(whoami)@<s21-tailscale-ip>:8022/$GIT_DIR"
echo ""
echo "Then push:"
echo "  git push s21 main"
echo ""
echo "Each push auto-deploys and restarts the agent."
echo ""
echo "To refresh only the hook after updating this repo on the S21:"
echo "  sed \"s|__GIT_DIR__|$GIT_DIR|g; s|__WORK_DIR__|$WORK_DIR|g\" \\"
echo "    $WORK_DIR/scripts/git-post-receive.sh > $GIT_DIR/hooks/post-receive"
echo "  chmod +x $GIT_DIR/hooks/post-receive"
