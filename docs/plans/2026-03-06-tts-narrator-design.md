# TTS Narrator pour AI-Intercom PWA

Date: 2026-03-06
Status: Approved

## Problem

Quand Claude Code travaille en arriere-plan (multi-sessions, plans longs), l'utilisateur n'a pas de feedback auditif sur l'avancement. Il doit regarder le PWA ou le terminal pour savoir ce qui se passe. L'idee est d'utiliser le service XTTS (Jetson Thor) pour narrer les evenements importants via le PWA.

## Architecture

```
Claude Code session
  |
  +-- Hooks (mecaniques) --> cc-heartbeat.sh --> /tmp/cc-sessions/{pid}.json
  |                                                    |
  |                                                    v
  |                                          AttentionMonitor (3s poll)
  |                                                    |
  +-- MCP tool (narratif) --> intercom_announce() -----+
  |                                                    |
  |                                                    v
  |                                          Hub API /api/attention/event
  |                                                    |
  |                                                    v
  |                                          AttentionStore
  |                                            +-- WebSocket -> PWA (JSON events)
  |                                            +-- Telegram (inchange)
  |
  |                                          Hub API /api/tts (proxy)
  |                                                    |
  |                                                    v
  |                                          Jetson Thor XTTS /v1/tts
  |                                                    |
  |                                                    v
  |                                          PWA: AudioContext.play(pcm)
```

Two announcement flows:
1. **Automatic** -- PWA receives existing WebSocket events (state_changed, new_session, session_ended) and generates text locally from templates
2. **Narrative** -- Claude pushes a message via `intercom_announce()`, hub broadcasts it, PWA synthesizes it

## New MCP tool: intercom_announce

```python
intercom_announce(
    message: str,          # "Phase 2 terminee, tous les tests passent"
    category: str = "milestone",  # milestone | difficulty | didactic
    priority: str = "normal",     # low | normal | high
)
```

Daemon forwards to hub via `POST /api/attention/announce`. Hub broadcasts via WebSocket:
```json
{
  "type": "tts_announce",
  "session_id": "...",
  "project": "coach-me",
  "message": "Phase 2 terminee, tous les tests passent",
  "category": "milestone",
  "priority": "normal"
}
```

## Event Categories

| Category | Source | Template example | Default |
|----------|--------|-----------------|---------|
| `attention` | Auto (state->WAITING) | "{project} attend ton input" | ON |
| `permission` | Auto (prompt.type=permission) | "{project} demande la permission d'executer {tool}" | ON |
| `milestone` | MCP tool | Free-form from Claude | ON |
| `difficulty` | MCP tool | Free-form from Claude | ON |
| `lifecycle` | Auto (new/ended) | "{project} demarre" / "{project} termine" | OFF |
| `didactic` | MCP tool | Free-form from Claude | OFF |
| `summary` | PWA timer | "2 sessions actives, coach-me travaille..." | OFF |

## Hub: TTS Proxy Endpoint

```
POST /api/tts
Body: {"text": "...", "language": "fr"}
Response: audio/raw (PCM 16-bit mono 24kHz)
```

Simple proxy to `XTTS_URL/v1/tts`. Rate-limited to max 1 request/2s.

## PWA: TTS Module

Responsibilities:
- **Announcement queue** -- FIFO with dedup (no duplicate message within 10s)
- **Audio fetch** -- `POST /api/tts` -> PCM -> `AudioContext` playback
- **Settings** (persisted in localStorage, synced via hub):
  - Toggle per category (6 toggles)
  - Volume
  - Verbosity: `minimal` (short templates) / `informatif` (full templates)
  - Global mute
- **Cooldown** -- minimum 5s between announcements (configurable)

## Automatic Templates

### Informatif verbosity

| Event | Template |
|-------|----------|
| state->WAITING + permission | "{project} demande la permission pour {tool}" |
| state->WAITING + question | "{project} pose une question" |
| state->WAITING + text_input | "{project} attend ton input" |
| new_session | "{project} demarre" |
| session_ended | "{project} termine" |
| summary (timer) | "{n} sessions actives. {details}" |

### Minimal verbosity

Reduced to: "coach-me attend" / "permission coach-me" / "coach-me demarre"

## CLAUDE.md Instructions

```
## TTS Announcements
When working on multi-step plans, use intercom_announce() to narrate major milestones,
difficulties, and (if didactic mode is enabled) what you're working on.
- milestone: completing a plan phase, tests passing, deployment done
- difficulty: 3rd retry on a failing test, API error, blocked
- didactic: brief explanation of current work (only if user enabled it)
Keep messages under 200 chars, in French, conversational tone.
```

## Changes Per Component

| Component | Change |
|-----------|--------|
| `src/daemon/mcp_server.py` | Add `intercom_announce` tool |
| `src/daemon/hub_client.py` | Add `push_announce()` method |
| `src/hub/hub_api.py` | Add `POST /api/attention/announce` endpoint |
| `src/hub/attention_api.py` | Add `POST /api/tts` proxy endpoint |
| `src/hub/attention_store.py` | Broadcast `tts_announce` events |
| `src/shared/models.py` | Add `TTSAnnounce` model |
| `pwa/` | TTS module (queue, fetch, playback, settings UI) |
| `CLAUDE.md` | Instructions for narrative announcements |
| `config/config.yml` | Add `xtts_url` for hub |

## Design Decisions

1. **PWA playback over local daemon** -- Works from any device, reuses existing WebSocket infra
2. **Hub proxies XTTS** -- Single URL for PWA, no CORS issues, rate-limiting centralized
3. **Dual flow (auto + narrative)** -- Mechanical events auto-interpreted, rich events pushed by Claude via MCP tool
4. **Configurable categories** -- Each category independently toggleable, verbosity level global
5. **Queue with cooldown** -- Prevents audio spam, dedup avoids repeats
