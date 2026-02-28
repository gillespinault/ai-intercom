# AI-Intercom

A distributed inter-agent communication system that enables AI coding agents (Claude Code, Codex, Gemini, etc.) running on different machines across a Tailscale network to communicate with each other through a Telegram-based message bus, with human-in-the-loop oversight via approval policies and forum-topic conversations.

## Architecture

```
 Machine A (Hub)                          Machine B (Daemon)
 +--------------------------+             +--------------------------+
 |  Telegram Supergroup     |             |                          |
 |  (forum topics/mission)  |             |  AI Agent (Claude, etc.) |
 |         ^                |             |       ^                  |
 |         |                |             |       | stdin/stdout     |
 |  +------+--------+       |             |  +----+----------+       |
 |  |  Telegram Bot |       |             |  | Agent Launcher |      |
 |  +------+--------+       |             |  +----+----------+       |
 |         |                |             |       |                  |
 |  +------+--------+       |   HTTP/HMAC |  +----+----------+       |
 |  |  Hub Router   +-------+---7700------+--+ Daemon API    |       |
 |  +------+--------+       |             |  +----+----------+       |
 |  |  Approval     |       |             |       |                  |
 |  |  Engine       |       |             |  +----+----------+       |
 |  +------+--------+       |             |  |  MCP Server   |       |
 |  |  Registry     |       |             |  |  (11 tools)    |       |
 |  |  (SQLite)     |       |             |  +---------------+       |
 |  +---------------+       |             |                          |
 +--------------------------+             +--------------------------+

 Machine C (Daemon)              Human
 +--------------------------+    +--------------------+
 |  AI Agent                |    | Telegram App       |
 |  MCP Server              |    | - Approve/deny     |
 |  Daemon API              |    | - Read transcripts |
 |  Hub Client      --------+--->| - /start_agent     |
 +--------------------------+    +--------------------+
```

**Hub** runs on one machine: Telegram bot, message router, approval engine, and SQLite registry.
**Daemons** run on every other machine: lightweight HTTP API, agent launcher, and MCP server exposing tools to AI agents.

## Features

- **Multi-agent communication** -- AI agents on different machines send messages and delegate tasks to each other
- **Human-in-the-loop** -- Every inter-agent message can require Telegram approval (once, per-mission, per-session, or always allow)
- **Telegram forum topics** -- Each mission gets its own forum topic for clean conversation threading
- **HMAC-SHA256 authentication** -- Per-machine tokens with timestamp anti-replay protection
- **Tailscale auto-discovery** -- Install script scans the Tailscale network to find the hub automatically
- **Interactive agent-to-agent chat** -- Asynchronous bidirectional messaging between active Claude Code sessions across machines via inbox queues and PostToolUse hooks
- **MCP integration** -- Eleven tools exposed via Model Context Protocol so any MCP-compatible agent can use the intercom
- **Agent launcher** -- Start AI agents on remote machines with mission context and path restrictions
- **Real-time mission feedback** -- Live streaming of agent activity (tools used, files read, commands run) via Telegram progress messages
- **Policy engine** -- Glob/regex-based approval rules with runtime grants (mission-level, session-level)
- **SQLite registry** -- Persistent machine and project tracking with heartbeat monitoring
- **Intelligent dispatcher** -- Send natural language messages in Telegram and Claude interprets and executes via MCP intercom tools
- **Docker support** -- Separate Compose files for hub and daemon deployment

## Quick Start

### Prerequisites

- Python 3.12+
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- A Telegram supergroup with forum topics enabled
- Tailscale installed on all machines (for auto-discovery)

### 1. Hub Setup (first machine)

```bash
git clone <repo-url> && cd AI-intercom
pip install -e .

# Interactive setup: creates ~/.config/ai-intercom/config.yml
./install.sh --init-hub

# Start the hub
ai-intercom standalone
```

The installer will prompt for your Telegram bot token, supergroup ID, and your Telegram user ID.

### 2. Daemon Setup (additional machines)

```bash
pip install -e .

# Auto-discovers hub on Tailscale, requests approval via Telegram
./install.sh

# After approval, start the daemon
ai-intercom daemon
```

Or specify the hub URL explicitly:

```bash
./install.sh --hub-url http://<hub-tailscale-ip>:7700
```

### 3. MCP Server (for AI agents)

Add to your agent's MCP configuration (e.g., `.mcp.json`):

```json
{
  "mcpServers": {
    "ai-intercom": {
      "command": "ai-intercom",
      "args": ["mcp-server", "--config", "~/.config/ai-intercom/config.yml"]
    }
  }
}
```

## MCP Tools Reference

| Tool | Description |
|------|-------------|
| `intercom_list_agents` | List available agents on the network. Filter by `"all"`, `"online"`, or `"machine:<id>"`. |
| `intercom_send` | Send a fire-and-forget message to another agent. |
| `intercom_ask` | Send a message and wait for a response (synchronous request/reply). |
| `intercom_start_agent` | Start an AI agent on a remote machine with a mission prompt. |
| `intercom_status` | Get the current status of a running mission. |
| `intercom_history` | Retrieve the full conversation history of a mission. |
| `intercom_register` | Update this agent's registry entry (description, capabilities, tags). |
| `intercom_report_feedback` | Report bugs, suggestions, or questions to the human operator. |
| `intercom_chat` | Send a message to an agent's active session. Creates a conversation thread. |
| `intercom_reply` | Reply to a message in an existing conversation thread. |
| `intercom_check_inbox` | Manually check for pending messages from other agents. |

## Configuration Reference

Configuration is loaded from `~/.config/ai-intercom/config.yml` with environment variable overrides.

### config.yml

```yaml
mode: standalone          # hub, daemon, or standalone (hub+daemon)

machine:
  id: "my-machine"        # Unique machine identifier
  display_name: "My Machine"
  description: "Description of this machine"

telegram:                  # Hub/standalone only
  bot_token: ""            # or TELEGRAM_BOT_TOKEN env var
  supergroup_id: 0         # or TELEGRAM_SUPERGROUP_ID env var
  security:
    allowed_users: []      # or TELEGRAM_OWNER_ID env var
    restrict_to_supergroup: true
    ignore_private_messages: true

hub:
  url: ""                  # Daemon: URL of the hub (or HUB_URL env var)
  listen: "0.0.0.0:7700"  # Hub: listen address

auth:
  token: ""                # Per-machine token (or INTERCOM_TOKEN env var)

discovery:                 # Auto-detect projects on this machine
  enabled: true
  scan_paths: []
  detect_by:
    - file: "CLAUDE.md"
    - file: ".git"
    - file: "AGENTS.md"
  exclude: ["node_modules", ".venv", "backup", "__pycache__"]

agent_launcher:
  default_command: "claude"
  default_args: ["-p", "--output-format", "json"]  # Automatically switched to stream-json for background missions
  allowed_paths: []        # Empty = allow all
  max_mission_duration: 1800

dispatcher:                  # Hub/standalone only
  enabled: false
  target: "serverlab/serverlab"  # machine/project to dispatch to
  system_prompt: |           # Prepended to every dispatched message
    Tu es le dispatcher Telegram AI-Intercom.
```

### Environment Variable Overrides

| Variable | Config Path | Description |
|----------|------------|-------------|
| `TELEGRAM_BOT_TOKEN` | `telegram.bot_token` | Telegram bot token |
| `TELEGRAM_SUPERGROUP_ID` | `telegram.supergroup_id` | Telegram supergroup chat ID |
| `TELEGRAM_OWNER_ID` | `telegram.security.allowed_users` | Authorized Telegram user ID |
| `HUB_URL` | `hub.url` | Hub URL for daemon connections |
| `INTERCOM_TOKEN` | `auth.token` | HMAC authentication token |

### Approval Policies

Policies are defined in `~/.config/ai-intercom/policies.yml`:

```yaml
defaults:
  require_approval: once       # never, always_allow, once, mission, session

rules:
  - from: "*"
    to: "*"
    type: ask
    message_pattern: "check|status|list|verify"
    approval: never
    label: "Read-only queries"
```

**Approval levels:**
- `never` -- Messages are delivered without any approval
- `always_allow` -- Auto-approved for this agent pair (session-persistent)
- `once` -- Requires Telegram approval for each message
- `mission` -- Approve once, then auto-approved for the rest of that mission
- `session` -- Approve once, then auto-approved for this agent pair until restart

## Intelligent Dispatcher

The dispatcher transforms the Telegram bot from a command-based router into a natural language interface. Instead of memorizing slash commands and machine/project names, you write plain messages and Claude handles the rest.

**How it works:**

1. You send a natural language message in the Telegram supergroup (outside any topic)
2. The bot shows a "Reflexion en cours..." indicator with typing animation
3. The message is dispatched to the configured target agent via `claude -p`
4. The thinking message is replaced with the response

**Example:**
> "List all online agents" → Claude calls `intercom_list_agents()` and returns the result

**Configuration (`config.yml`):**

```yaml
dispatcher:
  enabled: true
  target: "serverlab/serverlab"    # Agent that receives dispatched messages
  system_prompt: |
    Tu es le dispatcher Telegram AI-Intercom. Tu reponds de maniere concise.
    Utilise les outils MCP intercom pour communiquer avec les agents.
```

The dispatcher bypasses the router (no forum topic creation, no approval) and calls the target daemon directly. Add a policy rule for auto-approval of human messages:

```yaml
rules:
  - from: "human"
    to: "*"
    approval: never
    label: "Human dispatch - no approval needed"
```

## Security Model

1. **Per-machine HMAC tokens** -- Each machine receives a unique token (`ict_<machine>_<hex>`) during the join/approve flow. All daemon-to-hub and hub-to-daemon HTTP requests are signed with HMAC-SHA256.

2. **Timestamp anti-replay** -- Signed requests include a Unix timestamp. Requests older than 60 seconds are rejected.

3. **Telegram authorization** -- Only Telegram users in the `allowed_users` list can interact with the bot (approve messages, start agents, view status).

4. **Join approval** -- New machines must be approved via Telegram before receiving a token. The hub operator sees a notification and explicitly approves or denies.

5. **Path restrictions** -- The agent launcher can be configured with `allowed_paths` to restrict which directories agents can operate in.

## Docker Deployment

### Hub

```bash
cp config/config.example.yml config/config.yml
# Edit config/config.yml with your settings

docker compose -f docker-compose.hub.yml up -d
```

Environment variables can be set in a `.env` file:

```env
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_SUPERGROUP_ID=-1001234567890
TELEGRAM_OWNER_ID=123456789
INTERCOM_TOKEN=your-shared-token
```

### Daemon

```bash
docker compose -f docker-compose.daemon.yml up -d
```

```env
HUB_URL=http://<hub-ip>:7700
INTERCOM_TOKEN=your-machine-token
```

> **Important:** The daemon container needs access to Claude CLI credentials and the host filesystem for agent launching. Add to `docker-compose.daemon.yml`:
> ```yaml
> environment:
>   - HOME=/home/youruser
> volumes:
>   - /home/youruser:/home/youruser
> ```

Both containers expose port 7700 and use the same `Dockerfile`.

## Development

### Setup

```bash
git clone <repo-url> && cd AI-intercom
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### Running Tests

```bash
pytest --tb=short -q
```

### Project Structure

```
src/
  shared/
    models.py         # Message, AgentId, AgentInfo, MachineInfo (Pydantic)
    config.py         # YAML config loader with env var overrides
    auth.py           # HMAC-SHA256 sign/verify with anti-replay
  hub/
    main.py           # Hub entry point (Telegram bot + HTTP API)
    hub_api.py        # FastAPI: register, heartbeat, join, discover
    registry.py       # SQLite-backed machine/project registry
    router.py         # Message routing with approval checks
    approval.py       # Policy engine (glob/regex rules, runtime grants)
    telegram_bot.py   # Telegram bot (forum topics, commands, keyboards)
  daemon/
    main.py           # Daemon entry point (HTTP API + hub registration)
    api.py            # FastAPI: health, status, message receive
    hub_client.py     # HTTP client for hub communication
    agent_launcher.py # Subprocess agent launcher with path validation and stream-json feedback
    mcp_server.py     # FastMCP server exposing 11 intercom tools
  cli.py              # CLI entry point (hub/daemon/standalone/mcp-server)
  main.py             # Module entry point

tests/
  test_shared/        # Config, models, auth tests
  test_hub/           # Registry, approval, router, telegram, hub_api tests
  test_daemon/        # Daemon API, hub client, agent launcher, MCP tests
  test_integration/   # End-to-end message flow tests

config/
  config.example.yml  # Example configuration
  policies.example.yml # Example approval policies
```

## License

MIT License. See [LICENSE](LICENSE).

---

**Author:** Gilles Pinault
