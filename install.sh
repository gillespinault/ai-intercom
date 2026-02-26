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

# Request to join
echo "Requesting to join hub at $HUB_URL..."
RESPONSE=$(curl -sf -X POST "$HUB_URL/api/join" \
    -H "Content-Type: application/json" \
    -d "{\"machine_id\": \"$MACHINE_ID\", \"display_name\": \"$MACHINE_ID\"}" 2>/dev/null || true)

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
YAML

echo "Daemon config written to $CONFIG_DIR/config.yml"
echo "Start with: ai-intercom daemon --config $CONFIG_DIR/config.yml"
