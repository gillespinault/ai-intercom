# Changelog

All notable changes to AI-Intercom are documented here.

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
