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
      → idle_seconds from last_tool_time → state (WORKING/THINKING/WAITING)
      → When WAITING: capture terminal (PTY via pyte) → parse_terminal_output()
      → Pushes AttentionSession to Hub API
        → Hub broadcasts via WebSocket to PWA + Telegram notifications
```

**Prompt detection architecture** (see `docs/PROMPT-DETECTION-STATUS.md`):
- Terminal is the **sole source of truth** for prompt type
- Hooks only track activity timing (WORKING/THINKING/WAITING state)
- When WAITING: capture terminal → `parse_terminal_output()` → that's the prompt
- No `notification_data` in heartbeats; no hook-based prompt type inference

**Heartbeat script** (`scripts/cc-heartbeat.sh`):
- `detect_project()`: first checks `projects/*/` convention, then CLAUDE.md, then .git
- Special case: `~/.claude/skills/<name>/` → `skill:<name>`
- Actions: start, stop, working, activity
- `activity` action: always preserves `last_tool_time` (only touches file mtime for abandon detection)

**Abandon threshold** (`attention_monitor.py`):
- Sessions abandoned when BOTH `last_tool_time` AND heartbeat file mtime are older than `_ABANDON_THRESHOLD` (1 hour)
- Non-destructive: if the user returns and types, the session reappears automatically
- Zombie process detection via `/proc/{pid}/status` (Z state check)

**PWA Control Room** (`pwa/`):
- Tile grid layout with session cards showing state/prompt/actions
- Session disambiguation via tmux naming convention (`cc-<project>-<N>` → `#N` suffix)
- SKILL/SUB badges for background sessions (dimmed opacity)
- Dismiss button to hide resolved tiles
- WebSocket real-time updates from hub
- TTS narrator module (`pwa/tts.js`) for voice announcements via XTTS

## TTS Narrator — ALWAYS active

**At the start of every multi-step task**, check presence and user settings:

```bash
curl -s http://localhost:7700/api/attention/presence
```

Returns: `{"connected_clients": N, "tts": {"enabled": bool, "categories": {...}}}`

**Narrate only if** `connected_clients > 0` AND `tts.enabled == true`. Only announce categories where `tts.categories.<name>` is `true`.

- **milestone**: phase completed, tests passing, deployment done (1-2 per phase)
- **difficulty**: 3rd retry, unexpected error, blocked on something
- **didactic**: explain what you're doing and why (at major transitions)

**Rules:**
- French, conversational tone, max 200 chars
- Announce at EVERY significant step — not just start/end
- 1-2 announcements per plan phase minimum
- Don't wait until the end to announce — narrate as you go
- **Skip categories the user disabled** in PWA settings

**Examples:**
```python
intercom_announce(message="Je commence par ajouter l'endpoint de presence au hub", category="didactic")
intercom_announce(message="Tests OK, 2 sur 2. Je passe au deploiement", category="milestone")
intercom_announce(message="Erreur 502, le hostname ne resout pas dans Docker", category="difficulty")
```

If `connected_clients == 0` or `tts.enabled == false`, skip narration silently.

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
