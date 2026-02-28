# Design: Interactive Agent-to-Agent Chat

**Date**: 2026-02-28
**Status**: Approved
**Version**: AI-Intercom v0.3.0

## Problem

Currently, all inter-agent communication in AI-Intercom is **one-shot**: `intercom_ask` launches a new agent session, and `intercom_send` fires a message without expecting a response. There is no way for two agents already running in active Claude Code sessions to exchange messages directly.

## Goal

Enable **asynchronous bidirectional chat** between active Claude Code sessions across machines. Messages arrive in a queue and agents process them at natural breakpoints, without interrupting the human-agent conversation or launching new sessions.

## Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Initiation | Agent → Agent | Two active sessions exchange messages |
| Discovery | PostToolUse hook + manual tool | Hook fires on every tool call (~2-10s latency during active work), manual `intercom_check_inbox()` as fallback |
| Synchrony | Asynchronous | Messages queue up, agent processes when ready |
| Behavior on receive | Queue (not interrupt) | Agent treats at natural pause points |
| No active session | Inform sender | Sender gets `no_active_session`, decides next step |
| Session content exposure | Summary only | Status, current task summary, recent activity — not full conversation |

## Architecture

### Message Flow

```
Agent A (active session on serverlab)        Agent B (active session on limn)
    │                                            │
    │ intercom_chat("limn/mnemos", "hey")        │
    │                                            │
    ├──► MCP Server A                            │
    │      ├──► Hub /api/route (type="chat")     │
    │      │      ├──► Telegram (visibility)     │
    │      │      └──► Daemon B /api/session/deliver
    │      │                │                    │
    │      │                └──► inbox/session-B.jsonl
    │      │                                     │
    │      │                   [PostToolUse hook] │
    │      │                   reads inbox ───────┤
    │      │                   Agent B sees msg   │
    │      │                                     │
    │      │              intercom_reply(thread, "yo")
    │      │                                     │
    │      │     Hub ◄── MCP Server B ◄──────────┤
    │      │       ├──► Telegram (visibility)    │
    │      │       └──► Daemon A /api/session/deliver
    │      │              │                      │
    │  inbox/session-A.jsonl                     │
    │      │                                     │
    │ [PostToolUse hook]                         │
    │ Agent A sees reply                         │
```

### Human Experience

The human continues their normal conversation with the agent throughout. Chat messages appear as system context between tool calls and do not interrupt the human-agent dialogue. The human can also:
- See all inter-agent messages on Telegram
- Tell the agent "check tes mails" to force an inbox check
- Intervene if an agent says something wrong

## Components

### 1. Session Registration

When the MCP server starts, it registers the active session with the local daemon. When it stops, it unregisters.

**MCP Server startup:**
```python
POST http://localhost:7700/api/session/register
{
    "session_id": "s-20260228-a3f7b2",
    "project": "AI-intercom",
    "pid": 12345,
    "inbox_path": "~/.config/ai-intercom/inbox/s-20260228-a3f7b2.jsonl"
}
```

**MCP Server shutdown:**
```python
POST http://localhost:7700/api/session/unregister
{"session_id": "s-20260228-a3f7b2"}
```

**Daemon maintains:**
```python
active_sessions: dict[str, SessionInfo]
# SessionInfo: session_id, project, pid, inbox_path, registered_at
```

Session info is included in daemon→hub heartbeats so the hub knows which agents have active sessions.

### 2. Inbox File Format

One JSONL file per active session at `~/.config/ai-intercom/inbox/<session_id>.jsonl`:

```jsonl
{"thread_id":"t-abc123","from":"limn/mnemos","timestamp":"2026-02-28T16:30:00Z","message":"Hey, tu as l'URL du endpoint feedback ?","read":false}
{"thread_id":"t-abc123","from":"limn/mnemos","timestamp":"2026-02-28T16:31:22Z","message":"J'ai trouvé, merci quand même","read":false}
```

Fields:
- `thread_id`: Groups messages into a conversation thread
- `from`: Sender in `machine/project` format
- `timestamp`: ISO 8601
- `message`: The message content
- `read`: Marked `true` once delivered to the agent's context

### 3. Hooks

Two hooks installed in `~/.claude/settings.local.json`:

```json
{
  "hooks": {
    "PostToolUse": [{
      "command": "ai-intercom check-inbox --format hook",
      "timeout": 2000
    }],
    "UserPromptSubmit": [{
      "command": "ai-intercom check-inbox --format hook",
      "timeout": 2000
    }]
  }
}
```

**`check-inbox` behavior:**
1. Find inbox file for current session (via env var or PID matching)
2. If no file or no unread messages → exit silently (< 5ms)
3. If unread messages → print formatted output to stdout → marked as read

**Hook output format** (appears as `<system-reminder>` in agent context):
```
📨 Messages intercom en attente (2) :

[t-abc123] limn/mnemos (il y a 45s) :
  "Hey, tu as l'URL du endpoint feedback ?"

[t-abc123] limn/mnemos (il y a 12s) :
  "J'ai trouvé, merci quand même"

→ Utilise intercom_reply("t-abc123", "ta réponse") pour répondre.
```

**Performance budget:** < 5ms when inbox is empty (stat() on file only). No network calls.

### 4. New MCP Tools

Three new tools added to the MCP server:

**`intercom_chat(to, message)`** — Send a message to an active agent session.

```python
@mcp.tool()
async def intercom_chat(to: str, message: str) -> dict:
    """Send a message to an active agent session. Creates a conversation thread.
    Use intercom_list_agents() first to check if the target has an active session."""
    # Generate thread_id if new conversation
    # POST hub /api/route with type="chat"
    # Returns:
    #   {"thread_id": "t-xxx", "status": "delivered"} — message in inbox
    #   {"thread_id": "t-xxx", "status": "no_active_session"} — no session, consider intercom_ask
```

**`intercom_reply(thread_id, message)`** — Reply in an existing thread.

```python
@mcp.tool()
async def intercom_reply(thread_id: str, message: str) -> dict:
    """Reply to a message in an existing conversation thread."""
    # Resolves recipient from thread context
    # POST hub /api/route with type="chat", existing thread_id
    # Returns: {"status": "delivered|no_active_session"}
```

**`intercom_check_inbox()`** — Manually check for pending messages.

```python
@mcp.tool()
async def intercom_check_inbox() -> dict:
    """Check for pending messages from other agents. Same as the automatic
    hook but triggered explicitly (e.g. when user says 'check tes mails')."""
    # Read inbox file, return unread messages, mark as read
    # Returns: {"messages": [...], "count": N}
```

**Unchanged tools:** `intercom_ask`, `intercom_send`, `intercom_start_agent`, `intercom_status`, `intercom_history`, `intercom_register`, `intercom_report_feedback` — one-shot missions remain identical.

### 5. New Daemon Endpoints

```
POST /api/session/register
  → Register an active Claude Code session
  → Body: {session_id, project, pid, inbox_path}
  → Returns: 200 {status: "registered"}

POST /api/session/unregister
  → Unregister a session (on MCP server shutdown)
  → Body: {session_id}
  → Returns: 200 {status: "unregistered"}

POST /api/session/deliver
  → Deliver a chat message to a session's inbox
  → Body: {session_id?, project, thread_id, from_agent, message, timestamp}
  → Logic:
      1. Find active session for project (or by session_id)
      2. Verify PID is still alive (os.kill(pid, 0))
      3. Append message to inbox_path
      4. Return 200 {status: "delivered"}
  → Errors:
      - 404 {status: "no_active_session"} if no session or PID dead

GET /api/session/<session_id>/status
  → Get session status and summary
  → Returns: {project, status, summary, recent_activity, uptime, inbox_pending}
```

### 6. New Message Type

```python
class MessageType(StrEnum):
    ASK = "ask"
    SEND = "send"
    RESPONSE = "response"
    START_AGENT = "start_agent"
    STATUS = "status"
    CHAT = "chat"              # NEW
```

### 7. Hub Routing for Chat Messages

When the hub receives a `type="chat"` message:

1. Look up target daemon from registry
2. Check if daemon reports an active session for the target project (from heartbeat data)
3. If active session → `POST daemon/api/session/deliver` → return `"delivered"`
4. If no active session → return `"no_active_session"` to sender
5. Post to Telegram for human visibility (in both cases)

### 8. Enriched Heartbeat

Daemon→hub heartbeat includes active sessions:

```json
POST /api/heartbeat
{
    "machine_id": "serverlab",
    "status": "online",
    "active_sessions": [
        {
            "session_id": "s-20260228-a3f7b2",
            "project": "AI-intercom",
            "status": "working",
            "summary": "Designing interactive messaging feature",
            "uptime": "1h23m"
        }
    ]
}
```

### 9. Enriched Agent Listing

`intercom_list_agents()` response includes session info:

```json
{
    "agents": [
        {
            "machine": "limn",
            "project": "mnemos",
            "status": "online",
            "session": null
        },
        {
            "machine": "serverlab",
            "project": "AI-intercom",
            "status": "online",
            "session": {
                "status": "working",
                "summary": "Designing interactive messaging",
                "uptime": "1h23m"
            }
        }
    ]
}
```

Agents can see who is "chattable" (has an active session) vs who requires a one-shot mission.

### 10. Telegram Visibility

Chat messages appear in Telegram for human oversight:

**Outgoing:**
```
📨 Chat [t-abc123]
limn/mnemos → serverlab/AI-intercom
"Hey, tu as l'URL du endpoint feedback ?"
Session: ✅ délivré
```

**Reply:**
```
↩️ Reply [t-abc123]
serverlab/AI-intercom → limn/mnemos
"Oui, c'est POST /api/feedback sur le hub"
```

### 11. Install Integration

`install.sh` updated to:
1. Set up hooks in `~/.claude/settings.local.json` (PostToolUse + UserPromptSubmit)
2. Create inbox directory `~/.config/ai-intercom/inbox/`
3. Add `check-inbox` subcommand to CLI

## New Data Models

```python
class SessionInfo(BaseModel):
    session_id: str           # "s-YYYYMMDD-<6hex>"
    project: str              # "AI-intercom"
    pid: int                  # OS process ID of Claude Code
    inbox_path: str           # Path to inbox JSONL file
    registered_at: str        # ISO timestamp
    status: str = "active"    # "active" | "working" | "idle"
    summary: str = ""         # Current task summary
    recent_activity: list[str] = []  # Last N feedback items

class ThreadMessage(BaseModel):
    thread_id: str            # "t-<6hex>"
    from_agent: str           # "machine/project"
    timestamp: str            # ISO timestamp
    message: str              # Message content
    read: bool = False        # Marked true once delivered to agent context
```

## What Does NOT Change

- `intercom_ask` — still launches new agent for one-shot questions
- `intercom_send` — still fires one-way messages
- `intercom_start_agent` — still starts agent on remote machine
- `intercom_status` / `intercom_history` — still tracks missions
- Mission approval flow — unchanged
- Telegram bot commands — unchanged
- Agent launcher — unchanged (only used for one-shot missions)

## Summary of Changes by Component

| Component | Changes |
|-----------|---------|
| `src/daemon/mcp_server.py` | +3 tools, session register/unregister on startup/shutdown |
| `src/daemon/api.py` | +`/api/session/register`, `/api/session/unregister`, `/api/session/deliver`, `/api/session/<id>/status` |
| `src/daemon/main.py` | Active sessions table, inbox directory management |
| `src/hub/hub_api.py` | Route `type="chat"`, enriched heartbeat handling |
| `src/hub/registry.py` | Store active sessions per machine |
| `src/hub/telegram_bot.py` | Display chat messages in Telegram |
| `src/shared/models.py` | +`MessageType.CHAT`, +`SessionInfo`, +`ThreadMessage` |
| `src/cli.py` | +`check-inbox` subcommand |
| `install.sh` | Hook setup, inbox directory creation |
| `config/config.yml` | No changes needed |
