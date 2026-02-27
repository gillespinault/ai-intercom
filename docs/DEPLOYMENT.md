# Deployment Guide

Step-by-step instructions for deploying AI-Intercom on your own Tailscale network.

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.12+ | On all machines |
| Tailscale | Any | All machines on the same tailnet |
| Telegram bot | - | Created via [@BotFather](https://t.me/BotFather) |
| Telegram supergroup | - | With forum topics enabled |

### Telegram Setup

1. Message [@BotFather](https://t.me/BotFather) and create a bot (`/newbot`)
2. Save the bot token
3. Create a Telegram supergroup, enable "Topics" in group settings
4. Add your bot to the supergroup as admin
5. Get the supergroup ID: forward a message to [@userinfobot](https://t.me/userinfobot)
6. Get your user ID: message [@userinfobot](https://t.me/userinfobot) directly

## Architecture Overview

```
Hub Machine              Daemon Machine(s)        You
  ai-intercom              ai-intercom              Telegram
  standalone               daemon                   App
  (port 7700)              (port 7700)
       |                        |                     |
       +---- Tailscale VPN -----+                     |
       +------------- Telegram API -------------------+
```

- **One hub** runs on a central machine (standalone mode = hub + local daemon)
- **Daemons** run on every additional machine
- **You** approve and monitor via Telegram

## Option A: Native Install (Recommended)

### Step 1: Hub Machine

```bash
# Install
python3 -m venv ~/.local/share/ai-intercom/venv
~/.local/share/ai-intercom/venv/bin/pip install git+https://github.com/gillespinault/ai-intercom.git

# Interactive setup
./install.sh --init-hub
# Enter: bot token, supergroup ID, your Telegram user ID

# Start
~/.local/share/ai-intercom/venv/bin/ai-intercom standalone --config ~/.config/ai-intercom/config.yml
```

Verify: `curl http://localhost:7700/api/discover` should return `{"hub": true, ...}`.

### Step 2: Daemon Machines

On each additional machine:

```bash
# Install
python3 -m venv ~/.local/share/ai-intercom/venv
~/.local/share/ai-intercom/venv/bin/pip install git+https://github.com/gillespinault/ai-intercom.git

# Auto-discover hub on Tailscale and request to join
./install.sh
# Or specify hub URL:
./install.sh --hub-url http://<hub-tailscale-ip>:7700
```

The installer will:
1. Scan Tailscale peers to find the hub
2. Send a join request
3. You approve on Telegram (button appears in the supergroup)
4. Config + systemd service are created automatically

### Step 3: Verify

From any machine:
```bash
curl http://<hub-ip>:7700/api/agents | python3 -m json.tool
```

All machines and their projects should appear.

## Option B: Docker (Hub Only)

Docker is suitable for the hub. Daemons are better installed natively since they need to launch local AI agents.

```bash
git clone https://github.com/gillespinault/ai-intercom.git
cd ai-intercom

# Configure
cp config/config.example.yml config/config.yml
cp .env.example .env
# Edit .env with your Telegram credentials

# Start hub
docker compose -f docker-compose.hub.yml up -d
```

> **Note:** `network_mode: host` is required for Tailscale connectivity.

## Systemd Persistence

The `install.sh` script creates a systemd service automatically. To do it manually:

```bash
sudo tee /etc/systemd/system/ai-intercom.service > /dev/null <<EOF
[Unit]
Description=AI-Intercom
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$HOME
ExecStart=$HOME/.local/share/ai-intercom/venv/bin/ai-intercom daemon --config $HOME/.config/ai-intercom/config.yml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ai-intercom
```

Check status: `systemctl status ai-intercom`
View logs: `journalctl -u ai-intercom -f`

## MCP Configuration

To give your AI agents access to intercom tools, add to `.mcp.json` (project-level or `~/.mcp.json`):

```json
{
  "mcpServers": {
    "ai-intercom": {
      "command": "/path/to/venv/bin/ai-intercom",
      "args": ["mcp-server", "--config", "/path/to/.config/ai-intercom/config.yml"]
    }
  }
}
```

The MCP server auto-detects which project it's running in based on the working directory.

## Project Discovery

Daemons automatically discover projects by scanning `scan_paths` for `CLAUDE.md` or `.claude/` markers. A `home` project is always registered for general admin tasks.

To manually register projects, add them to your config:

```yaml
projects:
  - id: "my-project"
    description: "My cool project"
    capabilities: ["code", "web"]
    path: "/home/user/projects/my-project"
```

## Firewall Rules

AI-Intercom uses port 7700 (configurable via `hub.listen`). If you're using Tailscale with MagicDNS, no firewall changes are needed since Tailscale handles the routing.

If not using Tailscale:
```bash
# On hub machine
sudo ufw allow 7700/tcp

# On daemon machines
sudo ufw allow 7700/tcp
```

## Troubleshooting

### Daemon can't reach hub
```bash
# Verify Tailscale connectivity
ping <hub-tailscale-ip>
curl -sf http://<hub-tailscale-ip>:7700/api/discover
```

### Join request not appearing in Telegram
- Verify the bot is admin in the supergroup
- Verify `TELEGRAM_SUPERGROUP_ID` is correct (negative number)
- Check hub logs: `journalctl -u ai-intercom -f`

### Agent launch fails
- Check `allowed_paths` in config includes the project directory
- Verify `claude` (or configured command) is in PATH
- Check daemon logs for the specific error

### MCP server shows "home" instead of project name
- The MCP server detects the project from the current working directory
- Ensure your project has a `CLAUDE.md` or `.claude/` directory
- The project must be in one of the configured `scan_paths`
