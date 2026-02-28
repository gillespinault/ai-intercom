# Changelog

All notable changes to AI-Intercom are documented here.

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
