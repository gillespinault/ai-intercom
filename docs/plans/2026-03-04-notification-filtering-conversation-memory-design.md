# Design: Notification Filtering + Dispatcher Conversation Memory

**Date**: 2026-03-04
**Status**: Approved
**Version target**: v0.6.0
**Scope**: Two features — (1) Telegram attention alert filtering via PWA, (2) SQLite-backed dispatcher conversation memory

---

## Problem Statement

### Notification Spam
The attention pipeline sends Telegram notifications for every session entering WAITING state (permission, question, text_input, select_input). With multiple concurrent Claude Code sessions, this creates excessive Telegram noise. The PWA dashboard already shows all sessions — Telegram alerts should be selective.

### Stateless Dispatcher
Each Telegram message to the dispatcher is independent. No conversation history is maintained. Users cannot say "fais X" then "maintenant fais Y sur le meme serveur" — each message starts from zero context.

---

## Feature 1: Attention Alert Filtering

### Architecture

```
PWA Control Room                    Hub
+-----------------+    PATCH     +----------------------+
| Toggles:        | ----------> | notification_prefs   |
| [x] Permission  | /api/       | {                    |
| [ ] Question    | attention/  |   "permission": true |
| [ ] Text input  | prefs      |   "question": false  |
| [x] Select input|             |   "text_input": false|
+-----------------+  <WebSocket |   "select_input":true|
                     broadcast  +----------+-----------+
                                           |
                                  send_attention_notification()
                                           |
                                   if prefs[type] == false:
                                     -> skip Telegram
                                     -> still broadcast to PWA WebSocket
```

### Design Decisions

1. **Filter Telegram only** — PWA always receives all sessions via WebSocket (no filtering)
2. **Approvals/joins never filtered** — always sent to Telegram regardless of prefs
3. **Persistence** — `data/notification_prefs.json` (simple, no DB needed for 4 booleans)
4. **Default** — all types enabled (current behavior preserved)
5. **Single user** — no per-user prefs needed (single operator homelab)

### API

```
GET  /api/attention/prefs       -> { "permission": true, "question": true, ... }
PATCH /api/attention/prefs      <- { "question": false }  -> updated prefs
```

PATCH merges into existing prefs (partial update). Response includes full prefs. Hub broadcasts updated prefs to all PWA clients via WebSocket event `prefs_updated`.

### PWA UI

Settings panel accessible from the header (gear icon or "Notifications" label). Contains 4 toggles:
- Permission prompts (tool needs approval)
- Questions (multiple choice)
- Text input (free text)
- Select input (numbered options)

Each toggle sends PATCH on change. Visual feedback: active = coral accent, muted = dimmed.

### Files Impacted

| File | Changes |
|------|---------|
| `attention_store.py` | Load/save prefs, check before Telegram callback |
| `attention_api.py` | GET/PATCH `/api/attention/prefs` endpoints |
| `telegram_bot.py` | `send_attention_notification()` checks prefs |
| `pwa/index.html` | Settings panel with toggles + API calls |

---

## Feature 2: Dispatcher Conversation Memory

### Architecture

```
Telegram message (user_id, text)
       |
       v
 on_dispatch(text, user_id)
       |
       v
 ConversationStore (SQLite)
 +-----------------------------+
 | conversations table:        |
 |  id | user_id | role        |
 |     | content | timestamp   |
 |     | mission_id             |
 |                              |
 | get_history(user_id, N=10)  |
 | add_message(user_id, ...)   |
 | search(user_id, query, N=5) |
 | cleanup(older_than=48h)     |
 +-------------+---------------+
               |
               v
 Build prompt:
   system_prompt
   + conversation history (last 10 messages)
   + "User message:\n{text}"
               |
               v
 send_to_daemon(mission)
               |
               v
 Response -> store as 'assistant' message
          -> send to Telegram
```

### SQLite Schema

```sql
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    timestamp REAL NOT NULL,
    mission_id TEXT
);
CREATE INDEX idx_conv_user_ts ON conversations(user_id, timestamp DESC);
```

### Design Decisions

1. **SQLite** — survives restarts, lightweight, no external dependency. DB file at `data/conversations.db`
2. **Per-user history** — keyed on Telegram `user_id`
3. **Window of 10 messages** — last 10 (5 user + 5 assistant turns) injected into system prompt
4. **Token budget** — each historical message truncated to 500 chars in the injected context
5. **TTL 48 hours** — automatic cleanup of old messages on hub startup + periodic (every hour)
6. **Message format in prompt** — `[HH:MM] User: ...` / `[HH:MM] Assistant: ...` for clear turn demarcation

### MCP History Tool (Bonus)

The dispatcher agent (launched via `claude -p`) receives a tool description in its system prompt:

```
Tool: search_conversation_history
  query: str - search term
  limit: int (default 5) - max results
Returns: matching messages from the full conversation history (beyond the 10-message window)
```

Implementation: Hub exposes `GET /api/dispatcher/history?user_id=X&query=Y&limit=Z` — the dispatcher's system prompt instructs it to use `curl` or the intercom MCP to call this endpoint when it needs deeper context.

Alternative (simpler): instead of a real MCP tool, inject a note in the system prompt telling the dispatcher about the available endpoint, and let it decide when to fetch more context. This avoids MCP config complexity for the dispatcher agent.

### Prompt Construction

```python
def build_dispatcher_prompt(system_prompt: str, history: list[dict], current_message: str) -> str:
    parts = [system_prompt, "", "## Conversation history"]
    for msg in history:
        time_str = datetime.fromtimestamp(msg["timestamp"]).strftime("%H:%M")
        role = "User" if msg["role"] == "user" else "Assistant"
        content = msg["content"][:500]
        parts.append(f"[{time_str}] {role}: {content}")
    parts.append("")
    parts.append(f"User message:\n{current_message}")
    return "\n".join(parts)
```

### Configuration

```yaml
dispatcher:
  enabled: true
  target: "serverlab/home"
  system_prompt: |
    Tu es le dispatcher Telegram AI-Intercom...
  memory:
    enabled: true
    max_messages: 10        # messages in context window
    max_content_length: 500 # chars per historical message
    ttl_hours: 48           # auto-cleanup threshold
    db_path: "data/conversations.db"  # relative to hub working dir
```

### Files Impacted

| File | Changes |
|------|---------|
| New `src/hub/conversation_store.py` | ConversationStore class (SQLite CRUD + search + cleanup) |
| `src/hub/main.py` | Inject history in `on_dispatch()`, save user/assistant messages |
| `src/hub/hub_api.py` | `GET /api/dispatcher/history` endpoint for MCP history tool |
| `shared/config.py` | Parse `dispatcher.memory` config section |
| `config/config.yml` | Add `dispatcher.memory` defaults |

### Files NOT Impacted

- No daemon changes
- No MCP server changes
- No PWA changes (conversation memory is invisible to the dashboard)

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Prompt injection via history | Conversation history placed after system prompt, before user message. Truncation limits exposure. |
| Token budget overflow | Hard cap at 10 messages x 500 chars = 5000 chars max (~1500 tokens). Well within Claude's context. |
| SQLite concurrency | Single writer (hub process). Read-only from API endpoint. WAL mode for concurrent reads. |
| Notification prefs lost on crash | JSON file persisted on every PATCH. Loaded on startup. Defaults if missing. |

---

## Estimated Effort

| Component | Effort |
|-----------|--------|
| Notification prefs (hub API + store) | 1-2 hours |
| PWA settings panel | 1-2 hours |
| ConversationStore (SQLite) | 2-3 hours |
| Dispatcher prompt injection | 1-2 hours |
| History API endpoint | 1 hour |
| Config parsing | 30 min |
| Tests | 2-3 hours |
| **Total** | **~1 day** |
