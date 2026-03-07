# Roadmap

AI-Intercom enables AI coding agents across machines to communicate with each other and with humans through Telegram, with an Attention Hub for real-time session monitoring.

## Current: v0.7.0 (2026-03-06)

| Feature | Status |
|---------|--------|
| Hub + Daemon + MCP architecture | Done |
| Telegram bot (forum topics, approval, dispatcher) | Done |
| 13 MCP tools (send, ask, chat, reply, upgrade, announce, etc.) | Done |
| Real-time mission feedback (stream-json) | Done |
| Agent-to-agent chat with inbox hooks | Done |
| Attention Hub PWA (`/attention`) | Done |
| Heartbeat hooks (`cc-heartbeat.sh`) | Done |
| Push model (daemons push to hub) | Done |
| Version tracking + self-upgrade + network-wide upgrade | Done |
| GitHub Actions sync to dedicated repo | Done |
| Voice via Telegram (STT + TTS via limn) | Done (v0.5.0) |
| PWA Industrial Ops Console redesign | Done (v0.5.0) |
| Telegram notification filtering (per-prompt-type) | Done (v0.6.0) |
| Dispatcher conversation memory (SQLite) | Done (v0.6.0) |
| Terminal-only prompt detection (pyte) | Done (v0.7.0) |
| TTS Narrator (`intercom_announce` + PWA playback) | Done (v0.7.0) |

## Next Steps

Prioritized features extracted from [BACKLOG.md](BACKLOG.md).

### High Priority

| # | Feature | Description |
|---|---------|-------------|
| 1 | **Maintainer routing** | Automatic support routing per project. `intercom_support(service, issue)` routes to the responsible agent instead of just notifying the human. |

### Medium Priority

| # | Feature | Description |
|---|---------|-------------|
| 4 | **Response streaming** | Stream the final text response word-by-word in Telegram (activity streaming already works). |
| 5 | **Multi-target routing** | Dispatcher routes to multiple machines based on intent. Fan-out + result consolidation. |
| 6 | **Proactive notifications** | Agents notify humans unprompted (disk alerts, long mission completion, periodic reports). |
| 7 | **Dashboard enhancements** | Mission history, communication logs, full machine/project directory. Terminal viewer without tmux dependency. Deploy `cc-heartbeat.sh` on all machines via `install.sh`. |

### Low Priority / Exploration

| # | Feature | Description |
|---|---------|-------------|
| 8 | **Dispatcher HA** | Failover to another agent if the primary dispatcher machine is down. |
| 9 | **Mission queue** | Persistent queue (Redis/SQLite) with priority and automatic retry. |
| 10 | **Stronger auth** | Token rotation, mTLS via Tailscale certs. |
| 11 | **Metrics & observability** | Response times, token costs per mission, Prometheus/Grafana. |
