# Roadmap

AI-Intercom enables AI coding agents across machines to communicate with each other and with humans through Telegram, with an Attention Hub for real-time session monitoring.

## Current: v0.4.0 (2026-03-02)

| Feature | Status |
|---------|--------|
| Hub + Daemon + MCP architecture | Done |
| Telegram bot (forum topics, approval, dispatcher) | Done |
| 12 MCP tools (send, ask, chat, reply, upgrade, etc.) | Done |
| Real-time mission feedback (stream-json) | Done |
| Agent-to-agent chat with inbox hooks | Done |
| Attention Hub PWA (`/attention`) | Done |
| Heartbeat hooks (`cc-heartbeat.sh`) | Done |
| Push model (daemons push to hub) | Done |
| Version tracking + self-upgrade + network-wide upgrade | Done |
| GitHub Actions sync to dedicated repo | Done |

## Next Steps

Prioritized features extracted from [BACKLOG.md](BACKLOG.md).

### High Priority

| # | Feature | Description |
|---|---------|-------------|
| 1 | **Maintainer routing** | Automatic support routing per project. `intercom_support(service, issue)` routes to the responsible agent instead of just notifying the human. |
| 2 | **Voice via Telegram** | STT (Whisper) + TTS (CosyVoice) on limn (Jetson Thor). Voice messages in Telegram transcribed, dispatched, response synthesized back as voice. |
| 3 | **Dispatcher conversation memory** | Multi-turn conversations with context. Currently each Telegram message is independent. |

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
