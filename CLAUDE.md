# CLAUDE.md - AI-Intercom

## Project Overview

AI-Intercom is a multi-machine agent communication system. It connects AI agents (Claude Code, Codex, etc.) across machines via a hub-and-daemon architecture, with Telegram as the human interface.

**Architecture:**
- **Hub** (central, runs on main server): API server + Telegram bot + agent registry + mission router
- **Daemon** (one per machine): registers with hub, launches agents, reports status
- **MCP Server** (per agent session): exposes 11 intercom tools to Claude Code via MCP protocol

## Key Files

```
src/
├── cli.py                    # CLI entry point (hub/daemon/mcp-server subcommands)
├── main.py                   # Top-level entry
├── hub/
│   ├── main.py               # Hub startup, wires Telegram + API + router
│   ├── hub_api.py            # FastAPI endpoints (register, heartbeat, missions, feedback)
│   ├── telegram_bot.py       # Telegram bot (commands, callbacks, mission topics)
│   ├── registry.py           # Agent/machine registry (in-memory + persistence)
│   ├── router.py             # Message routing between agents
│   └── approval.py           # Mission approval logic
├── daemon/
│   ├── main.py               # Daemon startup
│   ├── api.py                # Daemon HTTP API (receive missions)
│   ├── hub_client.py         # HTTP client for hub API calls
│   ├── mcp_server.py         # MCP tool definitions (11 tools)
│   └── agent_launcher.py     # Launches Claude/Codex agents for missions
└── shared/
    ├── config.py             # Configuration model (YAML parsing)
    ├── models.py             # Shared data models (Message, SessionInfo, ThreadMessage)
    └── auth.py               # Token-based auth
```

Other key files:
- `config/config.yml` - Hub configuration (Telegram tokens, network settings)
- `docker-compose.hub.yml` - Hub Docker deployment
- `docker-compose.daemon.yml` - Daemon Docker deployment
- `install.sh` - Automated daemon installation script
- `data/feedback.jsonl` - Stored agent feedback

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
- Tests: `pytest` from project root
