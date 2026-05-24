#!/usr/bin/env bash
set -euo pipefail

# ── Cloudflare Tunnel Setup ──────────────────────────────────────────────
# Automates: install cloudflared, authenticate, create tunnel, configure
# DNS routing, and generate the config file + systemd/boot service.
#
# Works on:
#   - Termux (Android) — installs via pkg, sets up Termux:Boot auto-start
#   - macOS            — installs via Homebrew, sets up launchd service
#   - Linux            — installs via apt/yum/pacman or direct binary
#
# Usage:
#   bash scripts/setup-tunnel.sh                   # interactive setup
#   bash scripts/setup-tunnel.sh --domain example.com --name palmtop
#
# After setup, the tunnel auto-starts on boot and routes your domain to
# the agent's web channel (default: localhost:8000).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Defaults ─────────────────────────────────────────────────────────────
TUNNEL_NAME="${TUNNEL_NAME:-palmtop}"
DOMAIN="${DOMAIN:-}"
LOCAL_PORT="${LOCAL_PORT:-8000}"
WEBHOOK_PORT="${WEBHOOK_PORT:-}"  # optional: separate port for webhooks

# ── Colors ───────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}[info]${NC} $*"; }
ok()    { echo -e "${GREEN}[ok]${NC} $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC} $*"; }
error() { echo -e "${RED}[error]${NC} $*" >&2; }

# ── Platform detection ───────────────────────────────────────────────────
detect_platform() {
    if [ -n "${TERMUX_VERSION:-}" ] || [ -d "/data/data/com.termux" ]; then
        echo "termux"
    elif [[ "$(uname)" == "Darwin" ]]; then
        echo "macos"
    else
        echo "linux"
    fi
}

PLATFORM="$(detect_platform)"

# ── Parse arguments ──────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --domain) DOMAIN="$2"; shift 2 ;;
        --name) TUNNEL_NAME="$2"; shift 2 ;;
        --port) LOCAL_PORT="$2"; shift 2 ;;
        --webhook-port) WEBHOOK_PORT="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: setup-tunnel.sh [--domain DOMAIN] [--name TUNNEL_NAME] [--port PORT]"
            echo ""
            echo "Options:"
            echo "  --domain        Your domain (e.g., example.com)"
            echo "  --name          Tunnel name (default: palmtop)"
            echo "  --port          Local port to tunnel (default: 8000)"
            echo "  --webhook-port  Optional port for webhook subdomain"
            exit 0
            ;;
        *) error "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Install cloudflared ──────────────────────────────────────────────────
install_cloudflared() {
    if command -v cloudflared &>/dev/null; then
        ok "cloudflared already installed ($(cloudflared --version 2>&1 | head -1))"
        return 0
    fi

    info "Installing cloudflared..."
    case "$PLATFORM" in
        termux)
            pkg install -y cloudflared
            ;;
        macos)
            if command -v brew &>/dev/null; then
                brew install cloudflared
            else
                error "Homebrew not found. Install it first: https://brew.sh"
                exit 1
            fi
            ;;
        linux)
            if command -v apt-get &>/dev/null; then
                # Debian/Ubuntu
                curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
                echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
                sudo apt-get update && sudo apt-get install -y cloudflared
            elif command -v yum &>/dev/null; then
                sudo yum install -y cloudflared
            elif command -v pacman &>/dev/null; then
                sudo pacman -S --noconfirm cloudflared
            else
                # Direct binary install
                info "No package manager detected, installing binary directly..."
                ARCH="$(uname -m)"
                case "$ARCH" in
                    x86_64) ARCH="amd64" ;;
                    aarch64|arm64) ARCH="arm64" ;;
                    armv7l) ARCH="arm" ;;
                esac
                curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}" -o /usr/local/bin/cloudflared
                chmod +x /usr/local/bin/cloudflared
            fi
            ;;
    esac
    ok "cloudflared installed"
}

# ── Authenticate ─────────────────────────────────────────────────────────
authenticate() {
    local cert_path
    case "$PLATFORM" in
        termux) cert_path="$HOME/.cloudflared/cert.pem" ;;
        *)      cert_path="$HOME/.cloudflared/cert.pem" ;;
    esac

    if [ -f "$cert_path" ]; then
        ok "Already authenticated with Cloudflare"
        return 0
    fi

    info "Authenticating with Cloudflare..."
    echo ""
    echo "  A browser window will open. Log in and select the domain you want to use."
    echo "  If you're on a phone, copy the URL and open it in a browser."
    echo ""
    cloudflared tunnel login
    ok "Authenticated"
}

# ── Create tunnel ────────────────────────────────────────────────────────
create_tunnel() {
    # Check if tunnel already exists
    if cloudflared tunnel list 2>/dev/null | grep -q "$TUNNEL_NAME"; then
        ok "Tunnel '$TUNNEL_NAME' already exists"
        return 0
    fi

    info "Creating tunnel '$TUNNEL_NAME'..."
    cloudflared tunnel create "$TUNNEL_NAME"
    ok "Tunnel created"
}

# ── Get tunnel ID ────────────────────────────────────────────────────────
get_tunnel_id() {
    cloudflared tunnel list 2>/dev/null | grep "$TUNNEL_NAME" | awk '{print $1}' | head -1
}

# ── Configure DNS ────────────────────────────────────────────────────────
configure_dns() {
    if [ -z "$DOMAIN" ]; then
        echo ""
        echo -n "  Enter your domain (e.g., example.com): "
        read -r DOMAIN
        if [ -z "$DOMAIN" ]; then
            error "Domain is required for DNS routing"
            exit 1
        fi
    fi

    info "Routing $DOMAIN → tunnel '$TUNNEL_NAME'..."

    # Route the main domain
    cloudflared tunnel route dns "$TUNNEL_NAME" "$DOMAIN" 2>/dev/null || true
    ok "DNS route: $DOMAIN → $TUNNEL_NAME"

    # If webhook port specified, create a webhook subdomain
    if [ -n "$WEBHOOK_PORT" ]; then
        cloudflared tunnel route dns "$TUNNEL_NAME" "webhook.$DOMAIN" 2>/dev/null || true
        ok "DNS route: webhook.$DOMAIN → $TUNNEL_NAME (port $WEBHOOK_PORT)"
    fi
}

# ── Generate config file ─────────────────────────────────────────────────
generate_config() {
    local tunnel_id
    tunnel_id="$(get_tunnel_id)"

    if [ -z "$tunnel_id" ]; then
        error "Could not find tunnel ID for '$TUNNEL_NAME'"
        exit 1
    fi

    local config_dir="$HOME/.cloudflared"
    local config_file="$config_dir/config.yml"
    mkdir -p "$config_dir"

    info "Generating config at $config_file..."

    if [ -n "$WEBHOOK_PORT" ]; then
        # Multi-service config: main site + webhook endpoint
        cat > "$config_file" << EOF
tunnel: $tunnel_id
credentials-file: $config_dir/$tunnel_id.json

ingress:
  # Webhook endpoint (WhatsApp, Slack events, etc.)
  - hostname: webhook.$DOMAIN
    service: http://localhost:$WEBHOOK_PORT

  # Main web channel
  - hostname: $DOMAIN
    service: http://localhost:$LOCAL_PORT

  # Catch-all (required by cloudflared)
  - service: http_status:404
EOF
    else
        cat > "$config_file" << EOF
tunnel: $tunnel_id
credentials-file: $config_dir/$tunnel_id.json

ingress:
  - hostname: $DOMAIN
    service: http://localhost:$LOCAL_PORT
  - service: http_status:404
EOF
    fi

    ok "Config written to $config_file"
}

# ── Setup auto-start ─────────────────────────────────────────────────────
setup_autostart() {
    case "$PLATFORM" in
        termux)
            setup_termux_boot
            ;;
        macos)
            setup_launchd
            ;;
        linux)
            setup_systemd
            ;;
    esac
}

setup_termux_boot() {
    local boot_dir="$HOME/.termux/boot"
    mkdir -p "$boot_dir"

    cat > "$boot_dir/start-tunnel" << 'EOF'
#!/data/data/com.termux/files/usr/bin/bash
sleep 15  # let network settle after boot
exec cloudflared tunnel run palmtop
EOF
    sed -i "s/palmtop/$TUNNEL_NAME/" "$boot_dir/start-tunnel"
    chmod +x "$boot_dir/start-tunnel"
    ok "Termux:Boot script installed at $boot_dir/start-tunnel"
    echo "  Make sure Termux:Boot is installed and has been opened once."
}

setup_launchd() {
    local plist_dir="$HOME/Library/LaunchAgents"
    local plist_file="$plist_dir/com.palmtop.cloudflared.plist"
    mkdir -p "$plist_dir"

    local cf_path
    cf_path="$(which cloudflared)"

    cat > "$plist_file" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.palmtop.cloudflared</string>
    <key>ProgramArguments</key>
    <array>
        <string>$cf_path</string>
        <string>tunnel</string>
        <string>run</string>
        <string>$TUNNEL_NAME</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/cloudflared.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/cloudflared.err</string>
</dict>
</plist>
EOF

    launchctl unload "$plist_file" 2>/dev/null || true
    launchctl load "$plist_file"
    ok "launchd service installed and started"
    echo "  Tunnel will auto-start on login. Logs: /tmp/cloudflared.log"
}

setup_systemd() {
    if [ "$(id -u)" -eq 0 ]; then
        # System-level service
        cloudflared service install 2>/dev/null || true
        ok "systemd service installed (system-level)"
    else
        # User-level service
        local service_dir="$HOME/.config/systemd/user"
        local service_file="$service_dir/cloudflared-palmtop.service"
        mkdir -p "$service_dir"

        cat > "$service_file" << EOF
[Unit]
Description=Cloudflare Tunnel for Palmtop
After=network-online.target

[Service]
ExecStart=$(which cloudflared) tunnel run $TUNNEL_NAME
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

        systemctl --user daemon-reload
        systemctl --user enable cloudflared-palmtop
        systemctl --user start cloudflared-palmtop
        ok "systemd user service installed and started"
    fi
}

# ���─ Verify tunnel ────────────────────────────────────────────────────────
verify_tunnel() {
    echo ""
    info "Verifying tunnel..."
    if cloudflared tunnel info "$TUNNEL_NAME" &>/dev/null; then
        ok "Tunnel '$TUNNEL_NAME' is configured and ready"
    else
        warn "Could not verify tunnel — it may need a moment to propagate"
    fi
}

# ── Summary ──────────────────────────────────────────────────────────────
print_summary() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -e " ${GREEN}Cloudflare Tunnel setup complete!${NC}"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "  Tunnel name:  $TUNNEL_NAME"
    echo "  Domain:       $DOMAIN"
    echo "  Routes to:    localhost:$LOCAL_PORT"
    if [ -n "$WEBHOOK_PORT" ]; then
        echo "  Webhooks:     webhook.$DOMAIN → localhost:$WEBHOOK_PORT"
    fi
    echo "  Platform:     $PLATFORM"
    echo "  Auto-start:   enabled"
    echo ""
    echo "  To run manually:"
    echo "    cloudflared tunnel run $TUNNEL_NAME"
    echo ""
    echo "  To test:"
    echo "    curl https://$DOMAIN/health"
    echo ""
    if [ -n "$WEBHOOK_PORT" ]; then
        echo "  Webhook URL for WhatsApp/Slack:"
        echo "    https://webhook.$DOMAIN/webhook/whatsapp"
        echo ""
    fi
    echo "  Config: ~/.cloudflared/config.yml"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# ── Main ─────────────────────────────────────────────────────────────────
main() {
    echo ""
    echo "═══════════════════════════════════════════════════════════"
    echo " Palmtop — Cloudflare Tunnel Setup"
    echo " Platform: $PLATFORM"
    echo "═══════════════════════════════════════════════════════════"
    echo ""

    install_cloudflared
    authenticate
    create_tunnel
    configure_dns
    generate_config
    setup_autostart
    verify_tunnel
    print_summary
}

main
