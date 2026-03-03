# CLAUDE.md - AI-Intercom

## Project Overview

AI-Intercom is a multi-machine agent communication system. It connects AI agents (Claude Code, Codex, etc.) across machines via a hub-and-daemon architecture, with Telegram as the human interface and an Attention Hub for detecting when agents need human input.

**Architecture:**
- **Hub** (central, runs on main server): API server + Telegram bot + agent registry + mission router + attention store
- **Daemon** (one per machine): registers with hub, launches agents, reports status, monitors attention state
- **MCP Server** (per agent session): exposes 12 intercom tools to Claude Code via MCP protocol
- **Attention pipeline**: Claude Code hooks -> heartbeat files -> AttentionMonitor -> Hub API -> PWA WebSocket + Telegram notifications

## Key Files

```
src/
├── cli.py                       # CLI entry point (hub/daemon/mcp-server subcommands)
├── main.py                      # Top-level entry
├── hub/
│   ├── main.py                  # Hub startup, wires Telegram + API + router + attention
│   ├── hub_api.py               # FastAPI endpoints (register, heartbeat, missions, feedback, attention)
│   ├── telegram_bot.py          # Telegram bot (commands, callbacks, mission topics, /attention alerts)
│   ├── registry.py              # Agent/machine registry (in-memory + persistence)
│   ├── router.py                # Message routing between agents
│   ├── approval.py              # Mission approval logic
│   ├── attention_store.py       # In-memory session store + WebSocket broadcasting to PWA
│   └── attention_api.py         # REST + WebSocket endpoints for the attention dashboard
├── daemon/
│   ├── main.py                  # Daemon startup + AttentionMonitor sidecar
│   ├── api.py                   # Daemon HTTP API (receive missions)
│   ├── hub_client.py            # HTTP client for hub API calls (incl. push_attention_event)
│   ├── mcp_server.py            # MCP tool definitions (12 tools)
│   ├── upgrade.py               # Self-upgrade mechanism (detect, pull, install, restart)
│   ├── agent_launcher.py        # Launches Claude/Codex agents for missions
│   ├── attention_monitor.py     # Reads /tmp/cc-sessions/ heartbeats, detects state, pushes to hub
│   └── prompt_parser.py         # Parses tmux terminal output to detect Claude Code prompts
└── shared/
    ├── config.py                # Configuration model (YAML parsing)
    ├── models.py                # Shared data models (Message, AttentionSession, SessionInfo, etc.)
    └── auth.py                  # Token-based auth
```

Other key files:
- `config/config.yml` - Hub configuration (Telegram tokens, network settings)
- `docker-compose.hub.yml` - Hub Docker deployment
- `docker-compose.daemon.yml` - Daemon Docker deployment
- `install.sh` - Automated daemon installation script
- `data/feedback.jsonl` - Stored agent feedback
- `scripts/cc-heartbeat.sh` - Claude Code hook script writing heartbeats to `/tmp/cc-sessions/`
- `pwa/` - Attention Hub Progressive Web App (served at `/attention`)

## Attention Pipeline Details

The attention system detects when Claude Code sessions need human input:

```
Claude Code hooks (SessionStart/Stop/Notification/UserPromptSubmit)
  → cc-heartbeat.sh writes JSON to /tmp/cc-sessions/<PID>.json
    → AttentionMonitor polls heartbeat files every 3s
      → Captures tmux terminal via `tmux capture-pane`
        → prompt_parser.py detects prompt type (permission/question/text_input)
          → Pushes AttentionSession to Hub API
            → Hub broadcasts via WebSocket to PWA + Telegram notifications
```

**Prompt parser architecture** (`src/daemon/prompt_parser.py`):
- Priority cascade: permission → select_input → question → text_input
- SelectInput detection limited to bottom 30 lines (avoids scroll buffer history)
- Contiguous block extraction ensures only adjacent numbered options are collected
- Handles `\xa0` non-breaking spaces and ccusage statusline decorators
- `_prompt_changed()` compares question, command_preview, AND choices to detect updates

**Heartbeat script** (`scripts/cc-heartbeat.sh`):
- `detect_project()` walks up from CWD to find CLAUDE.md or .git
- Special case: `~/.claude/skills/<name>/` → `skill:<name>`
- Actions: start, stop, working, waiting, notification

**PWA Control Room** (`pwa/`):
- Tile grid layout with session cards showing state/prompt/actions
- Session disambiguation via tmux naming convention (`cc-<project>-<N>` → `#N` suffix)
- SKILL/SUB badges for background sessions (dimmed opacity)
- Dismiss button to hide resolved tiles
- WebSocket real-time updates from hub

## Commands

```bash
# Run hub
ai-intercom hub --config config/config.yml

# Run daemon
ai-intercom daemon --config ~/.config/ai-intercom/config.yml

# Run MCP server (used by Claude Code)
ai-intercom mcp-server --config ~/.config/ai-intercom/config.yml

# Check inbox for pending chat messages (used by hooks)
ai-intercom check-inbox --format hook
```

## When Launched as a Mission Agent

If you are launched by the intercom system to handle a support request or feedback:

1. **Read the mission prompt** - it contains the agent's question or feedback
2. **For usage questions**: Answer based on this CLAUDE.md and the `/intercom` skill
3. **For bug reports**: Investigate the relevant source file, explain the issue
4. **For suggestions**: Acknowledge, assess feasibility, note for the backlog
5. **Always respond concisely** - your output is relayed back to the requesting agent

## Development

- Python 3.12, uses `uv` for dependency management
- FastAPI for HTTP APIs, python-telegram-bot for Telegram
- MCP SDK (`mcp[cli]`) for MCP server
- Tests: `pytest` from project root (129 tests for daemon/hub/parser)

### After code changes

The daemon runs separately from the hub — both need updating:

```bash
# Rebuild hub (Docker)
docker compose -f docker-compose.hub.yml build --no-cache
docker compose -f docker-compose.hub.yml up -d

# Reinstall daemon (systemd, editable install from source)
/home/gilles/.local/share/ai-intercom-daemon/venv/bin/pip install -e .
sudo systemctl restart ai-intercom-daemon
```
