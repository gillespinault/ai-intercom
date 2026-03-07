# Design: Claude Code Usage Stats in Attention Hub

**Date**: 2026-03-04
**Status**: Approved
**Approach**: A — Daemon collects all data, pushes to hub

## Problem

The Attention Hub PWA shows active Claude Code sessions but lacks usage statistics. The user wants to see:
- **Global**: Current billing block progress (time-based %), countdown to reset, reset time, weekly token usage
- **Per-session**: Context window fill percentage (bar on each tile)
- **No costs displayed** (subscription user)

## Architecture

### Data Flow

```
Daemon (every 60s for ccusage, every 3s for context%)
  ├── ccusage blocks --json --offline → active block: endTime, remainingMinutes
  ├── ccusage weekly --json --offline → current week total tokens
  └── For each active session:
      └── Parse last assistant msg from JSONL → cache_read_input_tokens
          → context_percent = cache_read / 200,000 * 100

  → POST /api/attention/stats { block, weekly, sessions }
  → Hub stores in AttentionStore (in-memory)
  → Hub broadcasts WebSocket: { type: "usage_stats", ... }
  → PWA updates header + tiles
```

### Frequencies

- **Block/weekly stats**: Every 60 seconds (ccusage execution is ~1-2s)
- **Context % per session**: Every poll cycle (3s), lightweight JSONL tail read
- **PWA updates**: Real-time via existing WebSocket

## Data Structures

### UsageStats (pushed from daemon to hub)

```python
{
  "block": {
    "start_time": "2026-03-04T14:00:00.000Z",
    "end_time": "2026-03-04T19:00:00.000Z",
    "elapsed_pct": 68.0,          # time-based percentage
    "remaining_minutes": 96,
    "reset_time": "19:00",         # local HH:MM
    "is_active": true
  },
  "weekly": {
    "total_tokens": 1_209_063_077,
    "display": "1.2B"              # human-readable
  },
  "sessions": {
    "<session_id>": {
      "context_percent": 45.7,
      "context_tokens": 91398
    }
  }
}
```

### WebSocket Event

```json
{
  "type": "usage_stats",
  "stats": { /* UsageStats */ },
  "timestamp": "2026-03-04T15:30:00Z",
  "machine_id": "serverlab"
}
```

Also included in the initial `snapshot` event when PWA connects.

## PWA Design

### Header Stats Bar

```
◉ ATTENTION HUB    ██████████░░░░ 68% │ 1h36m → 19:00 │ W: 1.2B tok │ ●
```

- **Block progress bar**: Time-based (vert→jaune→rouge gradient)
- **Countdown**: Remaining time + reset hour
- **Weekly**: Compact token count for the week
- IBM Plex Mono, consistent with existing design system

### Per-Session Context Bar

Thin horizontal bar at bottom of each session tile:

```
┌─────────────────────────┐
│ AI-intercom #3          │
│ ● WORKING  idle: 5s     │
│ Tool: Bash              │
│ ▓▓▓▓▓▓▓▓░░░░░░ 45%     │
└─────────────────────────┘
```

Color thresholds (matching ccusage statusline defaults):
- **Green**: < 50%
- **Yellow**: 50-80%
- **Red**: > 80%

## Backend Changes

### Daemon: UsageCollector (new class in attention_monitor.py or separate file)

- `collect_block_stats()`: Runs `ccusage blocks --json --offline`, parses active block
- `collect_weekly_stats()`: Runs `ccusage weekly --json --offline`, extracts current week
- `get_context_percent(transcript_path)`: Reads last `type=assistant` line from JSONL, extracts `cache_read_input_tokens`, computes % of 200K
- Runs on 60s timer for ccusage commands
- Context % collected every poll cycle (3s) per active session

### Hub: Stats Storage

- `AttentionStore` gets `_usage_stats: dict` field
- New endpoint `POST /api/attention/stats` accepts daemon push
- Stats included in WebSocket `snapshot` on connect
- New `usage_stats` event type broadcast to subscribers

### Models: New dataclasses

- `BlockStats`: start_time, end_time, elapsed_pct, remaining_minutes, reset_time, is_active
- `WeeklyStats`: total_tokens, display
- `SessionContextStats`: context_percent, context_tokens
- `UsageStats`: block, weekly, sessions dict

### Hub Client: New method

- `push_usage_stats(stats)`: POST to `/api/attention/stats`

## YAGNI — Not Doing

- No stats persistence (in-memory only, recalculated on restart)
- No historical graphs
- No usage threshold alerts
- No multi-machine aggregation (daemon pushes its own stats)
- No cost display
- No block token limits (Max plan limits aren't exposed by ccusage)

## Files to Modify

1. `src/shared/models.py` — Add UsageStats dataclasses
2. `src/daemon/attention_monitor.py` — Add UsageCollector + integration
3. `src/daemon/hub_client.py` — Add push_usage_stats()
4. `src/hub/attention_store.py` — Store + broadcast stats
5. `src/hub/hub_api.py` or `src/hub/attention_api.py` — New /stats endpoint
6. `pwa/index.html` — Header stats bar HTML
7. `pwa/styles.css` — Stats bar + context bar styling
8. `pwa/app.js` — Handle usage_stats events, render bars
9. `tests/` — Tests for UsageCollector, stats endpoint
