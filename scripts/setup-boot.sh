#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

# Set up Termux:Boot to auto-start the agent on phone boot.
# Requires: Termux:Boot app installed from F-Droid.

BOOT_DIR="$HOME/.termux/boot"
AGENT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$BOOT_DIR"

cat > "$BOOT_DIR/start-pocket-agent" << EOF
#!/data/data/com.termux/files/usr/bin/bash
sleep 10  # let the system settle after boot
cd $AGENT_DIR
exec bash start.sh
EOF

chmod +x "$BOOT_DIR/start-pocket-agent"
echo "Boot script installed at $BOOT_DIR/start-pocket-agent"
echo "Make sure Termux:Boot is installed and has been opened once."
