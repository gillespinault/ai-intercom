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

## Option B: Docker

Docker works for both hub and daemon. For daemons, the container needs access to the host filesystem and Claude CLI credentials.

### Hub (Docker)

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

### Daemon (Docker)

The daemon container must mount the host home directory so agents can access project files and Claude CLI can find its credentials (`~/.claude/.credentials.json`).

```bash
# Edit .env
echo "HUB_URL=http://<hub-tailscale-ip>:7700" >> .env
echo "INTERCOM_TOKEN=<your-token>" >> .env

# Start daemon
docker compose -f docker-compose.daemon.yml up -d
```

The `docker-compose.daemon.yml` automatically mounts `$HOME` and sets the `HOME` environment variable. Verify with:

```bash
docker exec ai-intercom-daemon claude --version
docker exec ai-intercom-daemon ls ~/.claude/.credentials.json
```

> **Common issue:** If agent launches fail with "path is not in allowed_paths", verify that:
> 1. `HOME` is set correctly in the container (`docker exec ai-intercom-daemon env | grep HOME`)
> 2. The host home directory is mounted (`docker exec ai-intercom-daemon ls /home/youruser/`)
> 3. `allowed_paths` in config.yml includes your project directories

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

## Attention Hub Setup

The Attention Hub detects when Claude Code sessions are waiting for human input and notifies you via the PWA dashboard and Telegram. The pipeline is: **hooks -> heartbeat files -> AttentionMonitor -> Hub API -> PWA WebSocket + Telegram**.

### Step 1: Install via `install.sh` (Recommended)

The installer automatically sets up heartbeat hooks on each machine:

```bash
# On a machine that already has ai-intercom installed:
./install.sh --hub-url http://<hub-ip>:7700
```

The installer will:
1. Download `cc-heartbeat.sh` from the hub (`GET /api/scripts/cc-heartbeat.sh`)
2. Configure Claude Code hooks in `~/.claude/settings.json`
3. Create the sessions directory at `/tmp/cc-sessions/`

### Step 1b: Manual install (Alternative)

If you prefer manual setup, copy the script and configure hooks yourself:

```bash
# Copy the script
sudo cp scripts/cc-heartbeat.sh /usr/local/bin/cc-heartbeat.sh
sudo chmod +x /usr/local/bin/cc-heartbeat.sh
```

Dependencies: `jq` must be installed (`sudo apt install jq`).

### Step 2: Configure Claude Code hooks

If not using `install.sh`, add the following hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "cc-heartbeat.sh start" }]
      }
    ],
    "Stop": [
      {
        "hooks": [{ "type": "command", "command": "cc-heartbeat.sh stop" }]
      }
    ],
    "Notification": [
      {
        "matcher": "permission_prompt",
        "hooks": [{ "type": "command", "command": "cc-heartbeat.sh waiting" }]
      },
      {
        "hooks": [{ "type": "command", "command": "cc-heartbeat.sh notification" }]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [{ "type": "command", "command": "cc-heartbeat.sh working" }]
      }
    ]
  }
}
```

Each hook receives JSON on stdin from Claude Code (including `session_id` and `cwd`). The script writes a heartbeat file to `/tmp/cc-sessions/<pid>.json`.

The second Notification hook (without matcher) captures all notification payloads in the `notification_data` field, enabling prompt detection for sessions running without tmux.

### Step 3: Verify the pipeline

1. **Check heartbeat files** -- Start a Claude Code session, then:
   ```bash
   ls -la /tmp/cc-sessions/
   cat /tmp/cc-sessions/*.json
   ```
   You should see a JSON file with `pid`, `session_id`, `machine`, `project`, `last_tool`, and `last_tool_time`.

2. **Check the daemon picks up sessions** -- The daemon's `AttentionMonitor` reads `/tmp/cc-sessions/` every 3 seconds. Check daemon logs:
   ```bash
   journalctl -u ai-intercom -f | grep -i attention
   ```

3. **Check the PWA dashboard** -- Open `http://<hub-ip>:7700/attention` in a browser. Active sessions should appear with their state (WORKING, THINKING, or WAITING).

4. **Check Telegram notifications** -- When a session transitions to WAITING, the Telegram bot sends an alert in the supergroup. Use the `/attention` command in Telegram to see current WAITING sessions.

### How it works

| Component | Location | Role |
|-----------|----------|------|
| `cc-heartbeat.sh` | Each machine | Writes heartbeat JSON files on hook events |
| `/tmp/cc-sessions/*.json` | Each machine | Heartbeat files (one per Claude Code process) |
| `AttentionMonitor` | Daemon process | Reads heartbeats, detects state changes, pushes events to hub |
| `prompt_parser.py` | Daemon process | Parses tmux terminal output to extract prompt details |
| `attention_store.py` | Hub process | Aggregates sessions from all daemons, broadcasts via WebSocket |
| `attention_api.py` | Hub process | REST endpoints + WebSocket for the PWA |
| `pwa/` | Hub (served) | Browser dashboard at `/attention` |

> **Note:** Terminal viewing and prompt response require the Claude Code session to run inside tmux. Sessions not in tmux will still show state (WORKING/WAITING) but without terminal content.

## HTTPS Access

The Attention Hub dashboard should be exposed through the standard Traefik reverse proxy, **not** through `tailscale serve`.

### Setup (Traefik + VPS Nginx)

1. **Register the subdomain** on the VPS nginx API:
   ```bash
   curl -X POST "http://<vps-ip>:3004/add-subdomain" \
     -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{"subdomain": "attention.robotsinlove.be", "target": "100.80.12.35:443", "protocol": "https"}'
   ```

2. **Add a DNS A record** pointing `attention.robotsinlove.be` to the VPS IP (`185.158.132.168`).

3. **Create Traefik dynamic config** at `/etc/dokploy/traefik/dynamic/attention-hub.yml`:
   ```yaml
   http:
     routers:
       attention-router-websecure:
         rule: Host(`attention.robotsinlove.be`)
         service: attention-service
         entryPoints:
           - websecure
         tls:
           certResolver: letsencrypt
     services:
       attention-service:
         loadBalancer:
           servers:
             - url: http://127.0.0.1:7700
           passHostHeader: true
   ```
   Traefik auto-detects new files in its `dynamic/` directory (file provider).

4. **Verify**: `curl -I https://attention.robotsinlove.be` should return HTTP 200.

> **WARNING**: Never use `tailscale serve --bg <port>` without specifying `--https=<specific-port>`. The default maps port 443 and intercepts ALL HTTPS traffic on the Tailscale interface, which will take down every `*.robotsinlove.be` site behind Traefik.

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

### Agent launches with exit code 1 and empty stderr
- Check `HOME` is set correctly inside the container (Claude CLI needs `~/.claude/.credentials.json`)
- Run `docker exec ai-intercom-daemon claude -p "hello" --output-format stream-json --verbose` to test
- If using `--print` mode, `--verbose` is required for `stream-json` output (auto-added since v0.2.0)

### No feedback showing during missions
- Verify the daemon is running v0.2.0+ (check for `feedback` in `/api/missions/{id}` response)
- The daemon uses `stream-json` output format internally; the original `json` format in `default_args` is automatically switched
- Check daemon logs for stream parsing errors

### MCP server shows "home" instead of project name
- The MCP server detects the project from the current working directory
- Ensure your project has a `CLAUDE.md` or `.claude/` directory
- The project must be in one of the configured `scan_paths`

## Upgrading

### Self-Upgrade (per machine)

```bash
# Check current install info
ai-intercom self-upgrade --detect-only

# Upgrade to latest
ai-intercom self-upgrade
```

The self-upgrade mechanism:
1. Detects install method from `~/.config/ai-intercom/install.json`
2. Runs `git pull` if installed from a git repo
3. Runs `pip install -e .` (editable) or `pip install --upgrade` (pip)
4. Restarts the daemon via systemctl

### Network-Wide Upgrade (from any agent)

Use the `intercom_upgrade` MCP tool or the Hub API:

```bash
# Via Hub API - upgrade all machines
curl -X POST http://<hub>:7700/api/upgrade \
  -H "Content-Type: application/json" \
  -d '{"target": "outdated"}'
```

Target options:
- `"all"` -- Upgrade every machine
- `"outdated"` -- Only machines with version != hub version
- `"<machine_id>"` -- Specific machine

**Note:** Remote machines must already have v0.4.0+ installed for the `/api/upgrade` endpoint to exist. For the initial upgrade to v0.4.0, use manual `git pull && pip install -e . && systemctl restart` on each machine.

### Version Monitoring

After v0.4.0, each daemon reports its version in heartbeats:

```bash
curl http://<hub>:7700/api/agents?filter=all | python3 -c "
import json, sys
for a in json.load(sys.stdin)['agents']:
    mid = a.get('machine_id','?')
    ver = a.get('machine_version','')
    if mid not in seen: seen.add(mid); print(f'{mid}: {ver or \"unknown\"}')"
```
