#!/usr/bin/env bash
set -euo pipefail

# AI-Intercom installer
# Usage:
#   curl -fsSL .../install.sh | bash                    # Auto-discover hub
#   curl -fsSL .../install.sh | bash -s -- --init-hub   # Initialize first hub
#   curl -fsSL .../install.sh | bash -s -- --hub-url URL # Explicit hub

CONFIG_DIR="${HOME}/.config/ai-intercom"
HUB_URL=""
INIT_HUB=false
MACHINE_ID=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --init-hub) INIT_HUB=true; shift ;;
        --hub-url) HUB_URL="$2"; shift 2 ;;
        --machine-id) MACHINE_ID="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

echo "=== AI-Intercom Installer ==="
mkdir -p "$CONFIG_DIR"

# Detect machine ID from hostname if not provided
if [ -z "$MACHINE_ID" ]; then
    MACHINE_ID=$(hostname -s)
    echo "Machine ID: $MACHINE_ID (from hostname)"
fi

if [ "$INIT_HUB" = true ]; then
    echo "Initializing hub..."
    read -rp "Telegram Bot Token: " BOT_TOKEN
    read -rp "Telegram Supergroup ID: " GROUP_ID
    read -rp "Your Telegram User ID: " USER_ID

    cat > "$CONFIG_DIR/config.yml" <<YAML
mode: standalone
machine:
  id: "$MACHINE_ID"
  display_name: "$MACHINE_ID"
telegram:
  bot_token: "$BOT_TOKEN"
  supergroup_id: $GROUP_ID
  security:
    allowed_users: [$USER_ID]
    restrict_to_supergroup: true
    ignore_private_messages: true
hub:
  listen: "0.0.0.0:7700"
discovery:
  enabled: true
  scan_paths: ["$HOME"]
YAML

    echo "Hub config written to $CONFIG_DIR/config.yml"
    echo "Start with: ai-intercom standalone --config $CONFIG_DIR/config.yml"
    exit 0
fi

# Auto-discover hub on Tailscale if no URL provided
if [ -z "$HUB_URL" ]; then
    echo "Searching for AI-Intercom hub on Tailscale..."
    if command -v tailscale &> /dev/null; then
        PEERS=$(tailscale status --json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
for peer in data.get('Peer', {}).values():
    for ip in peer.get('TailscaleIPs', []):
        if ':' not in ip:  # IPv4 only
            print(ip)
" 2>/dev/null || true)

        for IP in $PEERS; do
            RESULT=$(curl -sf --connect-timeout 2 "http://$IP:7700/api/discover" 2>/dev/null || true)
            if echo "$RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d.get('hub')" 2>/dev/null; then
                HUB_URL="http://$IP:7700"
                echo "Found hub at $HUB_URL"
                break
            fi
        done
    fi

    if [ -z "$HUB_URL" ]; then
        echo "No hub found. Use --hub-url or --init-hub"
        exit 1
    fi
fi

# Detect Tailscale IP
TAILSCALE_IP=""
if command -v tailscale &> /dev/null; then
    TAILSCALE_IP=$(tailscale ip -4 2>/dev/null | head -1 || true)
    [ -n "$TAILSCALE_IP" ] && echo "Tailscale IP: $TAILSCALE_IP"
fi

# Request to join
echo "Requesting to join hub at $HUB_URL..."
RESPONSE=$(curl -sf -X POST "$HUB_URL/api/join" \
    -H "Content-Type: application/json" \
    -d "{\"machine_id\": \"$MACHINE_ID\", \"display_name\": \"$MACHINE_ID\", \"tailscale_ip\": \"$TAILSCALE_IP\"}" 2>/dev/null || true)

if echo "$RESPONSE" | grep -q "pending_approval"; then
    echo "Join request sent. Waiting for approval via Telegram..."
    echo "Check your Telegram supergroup for the approval notification."

    # Poll for approval
    for i in $(seq 1 60); do
        sleep 5
        TOKEN_RESP=$(curl -sf "$HUB_URL/api/join/status/$MACHINE_ID" 2>/dev/null || true)
        if echo "$TOKEN_RESP" | grep -q "approved"; then
            TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
            break
        fi
        echo -n "."
    done

    if [ -z "${TOKEN:-}" ]; then
        echo "Approval timed out. Try again or approve via Telegram."
        exit 1
    fi
else
    echo "Join failed: $RESPONSE"
    exit 1
fi

CLAUDE_PATH=$(which claude 2>/dev/null || echo "claude")

cat > "$CONFIG_DIR/config.yml" <<YAML
mode: daemon
machine:
  id: "$MACHINE_ID"
  display_name: "$MACHINE_ID"
hub:
  url: "$HUB_URL"
auth:
  token: "$TOKEN"
discovery:
  enabled: true
  scan_paths: ["$HOME"]
agent_launcher:
  default_command: "$CLAUDE_PATH"
  default_args: ["-p", "--output-format", "json"]
  allowed_paths: ["$HOME"]
  max_mission_duration: 1800
projects: []
YAML

echo "Daemon config written to $CONFIG_DIR/config.yml"

# --- Install systemd service ---
VENV_BIN=$(dirname "$(which ai-intercom 2>/dev/null || echo "")")
AI_INTERCOM_BIN="${VENV_BIN}/ai-intercom"

if [ -x "$AI_INTERCOM_BIN" ] && command -v systemctl &> /dev/null; then
    echo ""
    echo "=== Setting up systemd service ==="
    SERVICE_FILE="/etc/systemd/system/ai-intercom-daemon.service"
    CURRENT_USER=$(whoami)

    sudo tee "$SERVICE_FILE" > /dev/null <<SERVICE
[Unit]
Description=AI-Intercom Daemon ($MACHINE_ID)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$HOME
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$AI_INTERCOM_BIN daemon --config $CONFIG_DIR/config.yml
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=ai-intercom-daemon

[Install]
WantedBy=multi-user.target
SERVICE

    sudo systemctl daemon-reload
    sudo systemctl enable ai-intercom-daemon.service
    sudo systemctl start ai-intercom-daemon.service
    echo "Daemon service installed and started."
    echo "Check status: systemctl status ai-intercom-daemon"
    echo "View logs:    journalctl -u ai-intercom-daemon -f"
else
    echo ""
    echo "Start manually: ai-intercom daemon --config $CONFIG_DIR/config.yml"
fi

# --- MCP configuration for Claude Code ---
echo ""
echo "=== MCP Setup for Claude Code ==="
MCP_CONFIG='{
  "mcpServers": {
    "ai-intercom": {
      "command": "'$AI_INTERCOM_BIN'",
      "args": ["mcp-server", "--config", "'$CONFIG_DIR'/config.yml"]
    }
  }
}'

echo "Add this to your project's .mcp.json to enable intercom tools:"
echo "$MCP_CONFIG" | python3 -m json.tool 2>/dev/null || echo "$MCP_CONFIG"
echo ""
echo "=== Installation complete ==="
