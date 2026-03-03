# Changelog

All notable changes to AI-Intercom are documented here.

## [0.5.0] - 2026-03-03

### Added
- **PWA redesign** -- Industrial Ops Console aesthetic with IBM Plex Mono typography, dual accent system (coral/teal), scanline overlay, and radar brand animation
- **Sound alerts** -- Web Audio API two-tone alert (880Hz → 660Hz) when sessions enter WAITING state, with vibration on mobile (200ms-100ms-200ms pattern)
- **Toast notifications** -- Connection state changes, response confirmations, new/ended sessions shown as auto-dismissing toast overlays
- **Machine grouping** -- Sessions grouped by machine with collapsible headers, health badges, and collapse state persisted in localStorage
- **Event timeline** -- EKG-style dot strip per session showing last 10 state transitions (WORKING → THINKING → WAITING) with timestamps
- **Header stats** -- Live count badges in the header showing working/thinking/waiting session totals with colored dot indicators
- **Non-tmux session enrichment** -- `parse_notification_data()` extracts prompt info from Claude Code hook Notification payloads as fallback when tmux is unavailable
- **`notification_data` field** -- `AttentionHeartbeat` model includes hook notification payload for non-tmux prompt detection
- **Script serving endpoint** -- `GET /api/scripts/{name}` serves heartbeat scripts to remote daemons during installation
- **Stale session cleanup** -- `AttentionStore` periodically removes sessions not updated for >5 minutes, broadcasting `session_ended` events
- **`last_update` tracking** -- `AttentionSession` model tracks when each session was last updated by the hub
- **Install.sh hooks setup** -- Installer now downloads `cc-heartbeat.sh` from hub and configures Claude Code hooks (SessionStart, Stop, Notification, UserPromptSubmit)
- **11 new tests** -- `parse_notification_data` (10 tests), stale session cleanup (4 tests)

### Changed
- **Terminal theme** -- Enhanced ANSI color theme with bright variants (brightRed, brightGreen, etc.) matching the Industrial Ops Console palette
- **Tmux-centric UX** -- Sessions with tmux get full interaction (terminal + response area); sessions without tmux show "monitor only" badge with prompt info but no response controls
- **PWA service worker** -- Cache version bumped to v3 for redesigned assets
- **Manifest** -- Updated theme colors to match new dark palette (#0a0e1a)

### Fixed
- **HTTPS access** -- Documented proper setup via Traefik dynamic config + VPS nginx API instead of `tailscale serve` which caused port 443 conflict taking down all production HTTPS sites

## [0.4.0] - 2026-03-02

### Added
- **Push model architecture** -- Daemons push feedback/results to Hub instead of Hub polling daemons. Hub stores mission state as single source of truth.
- **Attention Hub PWA** -- Progressive Web App dashboard at `/attention` for monitoring agent sessions, viewing terminal content, and responding to prompts from mobile/desktop
- **Version tracking** -- Each daemon reports its version in heartbeat; `machine_version` field visible in `intercom_list_agents()` and `/api/agents`
- **Self-upgrade CLI** -- `ai-intercom self-upgrade` performs git pull + pip install + daemon restart. `--detect-only` shows install metadata.
- **Hub upgrade API** -- `POST /api/upgrade` dispatches upgrade commands to target daemons (`"all"`, `"outdated"`, or specific machine)
- **`intercom_upgrade` MCP tool** -- Trigger network-wide daemon upgrades from any agent session
- **Install metadata persistence** -- `~/.config/ai-intercom/install.json` stores install method, venv path, repo path for reliable self-upgrade
- **Attention monitor** -- Detects Claude Code sessions waiting for user input via tmux prompt detection
- **Heartbeat hooks** -- `scripts/cc-heartbeat.sh` integrates with Claude Code hooks (SessionStart, Stop, Notification, UserPromptSubmit) to write session heartbeat files into `/tmp/cc-sessions/`, enabling attention detection without tmux
- **GitHub Actions sync** -- Workflow syncs monorepo `projects/AI-intercom/` to dedicated `gillespinault/ai-intercom` repo on push

### Fixed
- Agent launcher now strips `CLAUDECODE` environment variable before launching subprocesses, preventing "nested session" detection failures on machines with active Claude Code sessions
- `push_attention_event` in daemon hub client was sending `{"event_type": ..., "session": ...}` but the hub expected `{"event": {"type": ..., "session": ...}}` -- corrected to match hub API schema
- Heartbeat hook PPID resolution: `$PPID` pointed to the ephemeral hook wrapper process instead of the actual Claude Code process; now resolves the grandparent PID via `ps -o ppid=`

### Changed
- `intercom_status` reads from Hub mission store (push model) instead of polling daemon directly
- `/intercom` skill updated with chat/ask/send decision tree

## [0.3.0] - 2026-02-28

### Added
- Interactive agent-to-agent chat via `intercom_chat()` and `intercom_reply()`
- `intercom_check_inbox()` tool for manual inbox checking
- Daemon session registration (register/unregister/deliver endpoints)
- PostToolUse and UserPromptSubmit hooks for automatic message delivery
- File-based inbox system (`~/.config/ai-intercom/inbox/`)
- `check-inbox` CLI subcommand for hook integration
- Session status endpoint (`/api/session/<id>/status`)
- Enriched heartbeat with active session info
- Enriched `intercom_list_agents()` showing active sessions
- Chat messages visible in Telegram for human oversight
- `MessageType.CHAT`, `SessionInfo`, `ThreadMessage` models
- MCP server definition added to `~/.mcp.json` global config

### Fixed (0.3.1 - 2026-03-01)
- `check-inbox` CLI crash: redundant `import os` in `cli.py` if/elif branches caused `UnboundLocalError` (Python treats `os` as local in entire function scope)
- Hardcoded version `"0.1.0"` in daemon and hub `/api/discover` endpoints replaced with dynamic `importlib.metadata.version("ai-intercom")`

## [0.2.1] - 2026-02-28

### Added
- **`/intercom` skill** -- Quick reference guide for all 8 MCP tools + SAV support channel for agents
- **Feedback Telegram notifications** -- `POST /api/feedback` now sends instant Telegram notifications with type-based emoji (bug/improvement/note)
- **Skill distribution endpoint** -- `GET /api/skill/intercom` serves the skill file for remote installation
- **CLAUDE.md** -- Project context file for mission-launched agents

### Changed
- `install.sh` now downloads and installs the `/intercom` skill on target machines
- Dockerfile includes `.claude/commands/` for skill distribution from container

## [0.2.0] - 2026-02-28

### Added
- **Granular mission feedback** -- Real-time streaming of agent activity during missions via `--output-format stream-json`
- `FeedbackItem` dataclass tracking each tool use, text output, and turn count
- `launch_streaming()` method reading Claude CLI output line-by-line instead of blocking on `proc.communicate()`
- `TOOL_LABELS` mapping for 12 tool types with emoji and French labels
- `_summarize_tool_input()` for extracting short details (file paths, commands, patterns)
- Telegram progress messages showing live agent activity (tools used, files read, commands run)
- `feedback_since` cursor on `/api/missions/{id}` for incremental polling
- 13 new unit tests for feedback parsing, tool summarization, and streaming

### Fixed
- `--verbose` flag now auto-added when using `stream-json` with `--print` mode (required by Claude CLI)
- 404 errors handled gracefully in mission tracker polling (prevents zombie polling loops)

### Docker
- Daemon containers should mount the host home directory and set `HOME` environment variable for Claude CLI credential access

## [0.1.0] - 2026-02-27

### Added
- Hub with Telegram bot integration (forum topics, approval keyboards, `/start_agent` command)
- Daemon with HTTP API, agent launcher, and hub registration
- MCP server exposing 7 intercom tools (`list_agents`, `send`, `ask`, `start_agent`, `status`, `history`, `register`)
- HMAC-SHA256 authentication with per-machine tokens and anti-replay protection
- Policy-based approval engine with glob/regex pattern matching
- SQLite-backed machine and project registry with heartbeat monitoring
- Tailscale auto-discovery in `install.sh`
- Join/approve flow via Telegram inline keyboards
- Standalone mode (hub + daemon in single process)
- Auto-discovery of projects via `CLAUDE.md` / `.claude/` markers
- "Home" agent auto-registration for admin tasks outside any project
- MCP server auto-detects current project from working directory
- Configurable hub listen address (`hub.listen`)
- Docker Compose files for hub and daemon deployment
- GitHub Actions CI (Python 3.12/3.13, lint, test, Docker build)
- Systemd service installation via `install.sh`
- 65 unit and integration tests
