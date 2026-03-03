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

# --- Install heartbeat hooks for Attention Hub ---
echo ""
echo "=== Installing Attention Hub heartbeat hooks ==="
HEARTBEAT_DIR="${HOME}/.config/ai-intercom"
HEARTBEAT_SCRIPT="${HEARTBEAT_DIR}/cc-heartbeat.sh"

# Download heartbeat script from hub
HEARTBEAT_CONTENT=$(curl -sf "$HUB_URL/api/scripts/cc-heartbeat.sh" 2>/dev/null || true)
if [ -n "$HEARTBEAT_CONTENT" ]; then
    echo "$HEARTBEAT_CONTENT" > "$HEARTBEAT_SCRIPT"
    chmod +x "$HEARTBEAT_SCRIPT"
    echo "Heartbeat script installed at $HEARTBEAT_SCRIPT"
else
    echo "Could not download heartbeat script from hub. Skipping."
fi

# Configure heartbeat hooks in settings.json
CLAUDE_SETTINGS="${HOME}/.claude/settings.json"
if [ -f "$CLAUDE_SETTINGS" ] && [ -f "$HEARTBEAT_SCRIPT" ]; then
    if ! grep -q "cc-heartbeat" "$CLAUDE_SETTINGS" 2>/dev/null; then
        echo "Adding heartbeat hooks to $CLAUDE_SETTINGS"
        python3 -c "
import json
with open('$CLAUDE_SETTINGS') as f:
    settings = json.load(f)
hooks = settings.setdefault('hooks', {})
hb = '$HEARTBEAT_SCRIPT'
hooks['SessionStart'] = [{'hooks': [{'type': 'command', 'command': 'bash ' + hb + ' start'}]}]
hooks['Stop'] = [{'hooks': [{'type': 'command', 'command': 'bash ' + hb + ' stop'}]}]
hooks.setdefault('Notification', [])
if not any('waiting' in str(h) for h in hooks['Notification']):
    hooks['Notification'].insert(0, {'matcher': 'permission_prompt', 'hooks': [{'type': 'command', 'command': 'bash ' + hb + ' waiting'}]})
if not any('notification' in str(h) for h in hooks['Notification']):
    hooks['Notification'].append({'hooks': [{'type': 'command', 'command': 'bash ' + hb + ' notification'}]})
hooks['UserPromptSubmit'] = [{'hooks': [{'type': 'command', 'command': 'bash ' + hb + ' working'}]}]
with open('$CLAUDE_SETTINGS', 'w') as f:
    json.dump(settings, f, indent=2)
print('Heartbeat hooks installed.')
" 2>/dev/null || echo "Could not auto-configure heartbeat hooks."
    else
        echo "Heartbeat hooks already configured."
    fi
fi

# Create sessions directory
mkdir -p /tmp/cc-sessions
echo "Sessions directory: /tmp/cc-sessions"

# --- Install /intercom skill for Claude Code ---
echo ""
echo "=== Installing /intercom skill ==="
SKILL_DIR="${HOME}/.claude/commands"
mkdir -p "$SKILL_DIR"

if [ -n "$HUB_URL" ]; then
    # Download skill from hub
    SKILL_CONTENT=$(curl -sf "$HUB_URL/api/skill/intercom" 2>/dev/null || true)
fi

if [ -z "${SKILL_CONTENT:-}" ]; then
    # Fallback: create minimal skill pointing to /intercom docs
    SKILL_CONTENT='---
name: intercom
description: AI-Intercom quick reference and support channel.
---

# AI-Intercom - Quick Reference

Run `intercom_list_agents(filter="online")` to discover agents.
Run `intercom_ask(to="machine/project", message="your task")` to send a mission.
Run `intercom_report_feedback(type="bug", description="...")` for support.

Full docs: https://github.com/your-org/AI-intercom'
fi

echo "$SKILL_CONTENT" > "$SKILL_DIR/intercom.md"
echo "Skill installed at $SKILL_DIR/intercom.md"

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

# Auto-add to ~/.mcp.json
MCP_FILE="${HOME}/.mcp.json"
if [ -f "$MCP_FILE" ]; then
    if grep -q "ai-intercom" "$MCP_FILE" 2>/dev/null; then
        echo "ai-intercom already in $MCP_FILE (skipping)"
    else
        python3 -c "
import json
with open('$MCP_FILE') as f:
    data = json.load(f)
data.setdefault('mcpServers', {})['ai-intercom'] = {
    'command': '$AI_INTERCOM_BIN',
    'args': ['mcp-server', '--config', '$CONFIG_DIR/config.yml']
}
with open('$MCP_FILE', 'w') as f:
    json.dump(data, f, indent=2)
print('Added ai-intercom to ' + '$MCP_FILE')
"
    fi
else
    echo "$MCP_CONFIG" > "$MCP_FILE"
    echo "Created $MCP_FILE with ai-intercom MCP server"
fi

# --- Hook setup for interactive chat ---
echo ""
echo "=== Setting up chat hooks ==="
INBOX_DIR="${HOME}/.config/ai-intercom/inbox"
mkdir -p "$INBOX_DIR"

SETTINGS_FILE="${HOME}/.claude/settings.local.json"
if [ -f "$SETTINGS_FILE" ]; then
    # Add hooks if not already present
    if ! grep -q "check-inbox" "$SETTINGS_FILE" 2>/dev/null; then
        echo "Adding PostToolUse and UserPromptSubmit hooks to $SETTINGS_FILE"
        python3 -c "
import json
with open('$SETTINGS_FILE') as f:
    settings = json.load(f)
hooks = settings.setdefault('hooks', {})
check_cmd = 'ai-intercom check-inbox --format hook'
for hook_name in ['PostToolUse', 'UserPromptSubmit']:
    existing = hooks.get(hook_name, [])
    if not any(check_cmd in str(h) for h in existing):
        existing.append({'command': check_cmd, 'timeout': 2000})
    hooks[hook_name] = existing
with open('$SETTINGS_FILE', 'w') as f:
    json.dump(settings, f, indent=2)
" 2>/dev/null && echo "Hooks installed." || echo "Could not auto-configure hooks. Add manually."
    else
        echo "Hooks already configured."
    fi
else
    echo "No settings.local.json found. Hooks will need manual configuration."
fi

# --- Install claude-tmux wrapper for Attention Hub ---
echo ""
echo "=== Installing claude-tmux wrapper ==="
CLAUDE_TMUX_SCRIPT="${HOME}/.local/bin/claude-tmux"
mkdir -p "${HOME}/.local/bin"

# Download from hub or use local copy
CLAUDE_TMUX_CONTENT=$(curl -sf "$HUB_URL/api/scripts/claude-tmux.sh" 2>/dev/null || true)
if [ -n "$CLAUDE_TMUX_CONTENT" ]; then
    echo "$CLAUDE_TMUX_CONTENT" > "$CLAUDE_TMUX_SCRIPT"
    chmod +x "$CLAUDE_TMUX_SCRIPT"
    echo "claude-tmux installed at $CLAUDE_TMUX_SCRIPT"
else
    echo "Could not download claude-tmux from hub. Skipping."
fi

# Add shell function to .bashrc if not already present
SHELL_RC=""
if [ -f "$HOME/.zshrc" ]; then
    SHELL_RC="$HOME/.zshrc"
elif [ -f "$HOME/.bashrc" ]; then
    SHELL_RC="$HOME/.bashrc"
fi

if [ -n "$SHELL_RC" ] && [ -f "$CLAUDE_TMUX_SCRIPT" ]; then
    if ! grep -q "claude-tmux" "$SHELL_RC" 2>/dev/null; then
        echo "" >> "$SHELL_RC"
        echo "# AI-Intercom: auto-wrap claude in tmux for Attention Hub support" >> "$SHELL_RC"
        echo "alias claude='claude-tmux'" >> "$SHELL_RC"
        echo "Shell alias added to $SHELL_RC (claude -> claude-tmux)"
        echo "Run 'source $SHELL_RC' or start a new terminal to activate."
    else
        echo "claude-tmux alias already configured in $SHELL_RC"
    fi
fi

# --- Save install metadata for self-upgrade ---
echo ""
echo "=== Saving install metadata ==="
INSTALL_JSON="$CONFIG_DIR/install.json"
cat > "$INSTALL_JSON" <<INST
{
  "method": "pip",
  "venv": "$(dirname "$(dirname "$AI_INTERCOM_BIN")" 2>/dev/null || echo "")",
  "repo": "",
  "binary": "$AI_INTERCOM_BIN"
}
INST
echo "Install metadata saved to $INSTALL_JSON"

echo ""
echo "=== Installation complete ==="
