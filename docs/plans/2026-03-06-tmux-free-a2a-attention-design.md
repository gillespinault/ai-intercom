# Design: Tmux-Free Attention Architecture (A2A-Native)

**Date**: 2026-03-06
**Status**: Validated
**Replaces**: tmux capture-pane / send-keys pipeline
**Integrates with**: A2A protocol migration roadmap

## Problem Statement

The Attention Hub monitors Claude Code sessions and allows humans to respond to prompts (permissions, questions) from a PWA or Telegram. Currently this requires **tmux** as an intermediary:

- `tmux capture-pane` reads terminal content (30 lines)
- `prompt_parser.py` extracts prompt structure from raw ANSI output
- `tmux send-keys` injects keystroke responses

tmux creates a **full terminal multiplexer layer** that:
- Conflicts with MobaXterm keybindings and UX
- Adds complexity for every machine (install tmux, use wrapper script)
- Is fundamentally a **terminal scraping hack** incompatible with the A2A vision

## Key Discovery: PermissionRequest Hook

Claude Code provides a `PermissionRequest` hook that fires **before** the permission dialog is shown to the user. It receives structured data and can **return a decision programmatically**:

```json
// Input to hook (POST body from Claude Code)
{
  "session_id": "abc123",
  "tool_name": "Bash",
  "tool_input": { "command": "docker ps", "description": "List containers" },
  "permission_suggestions": [{ "type": "toolAlwaysAllow", "tool": "Bash" }]
}

// Hook responds with (HTTP 200 JSON body)
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {
      "behavior": "allow"
    }
  }
}

// To deny:
{
  "hookSpecificOutput": {
    "hookEventName": "PermissionRequest",
    "decision": {
      "behavior": "deny",
      "reason": "Denied by remote approval"
    }
  }
}
```

**Additional decision fields** (optional):
- `updatedInput`: modify tool input before execution (allow only)
- `permissionRule`: register a rule so the user isn't asked again
- `reason`: explanation string
- `interrupt`: if true + deny, stops Claude entirely

This is the **native Claude Code API** for programmatic permission control. Combined with HTTP hooks (Claude Code can POST to a URL and wait for the response), this enables a fully structured approval flow without any terminal interaction.

## Architecture: Before vs After

### Before (tmux-based)

```
Claude Code session (inside tmux)
  |
  v
cc-heartbeat.sh hooks --> /tmp/cc-sessions/{pid}.json
  |
  v
AttentionMonitor polls heartbeat files every 3s
  |
  v
tmux capture-pane -t <session>  --> raw terminal text (30 lines)
  |
  v
prompt_parser.py  --> PromptInfo (type, question, choices, tool, command_preview)
  |
  v
Hub API --> PWA WebSocket + Telegram notification
  |
  v (user responds)
  |
Hub API --> Daemon --> tmux send-keys -t <session> <keys> Enter
```

**Problems**: terminal scraping, ANSI parsing, cursor position tracking, tmux dependency on every machine, MobaXterm conflicts, fragile regex parsing.

### After (A2A-native, hook-based)

```
Claude Code session (plain terminal, no tmux)
  |
  +-- PermissionRequest hook --> HTTP POST to daemon:7331/hook/permission
  |     daemon forwards to hub, hub notifies PWA/Telegram
  |     human responds, hub returns decision
  |     hook returns { hookSpecificOutput: { decision: { behavior: "allow"|"deny" } } }
  |     Claude Code continues automatically
  |
  +-- Notification hook --> cc-heartbeat.sh "notification"
  |     writes structured notification_data to heartbeat file
  |     AttentionMonitor reads heartbeat, pushes to hub
  |     PWA shows session state (working/thinking/waiting)
  |
  +-- SessionStart/Stop hooks --> cc-heartbeat.sh "start"|"stop"
        session lifecycle tracking (unchanged)
```

**No tmux. No terminal scraping. No ANSI parsing. Structured protocol end-to-end.**

## Detailed Design

### Layer 1: Permission Approval (replaces 80% of tmux use)

Permission prompts ("Allow Bash: docker ps?") are the dominant interaction in the PWA. They are fully solvable via the PermissionRequest hook.

**Flow**:

1. Claude Code encounters a tool that needs permission
2. `PermissionRequest` hook fires with `tool_name`, `tool_input`
3. Hook is an HTTP hook: `POST http://localhost:{DAEMON_PORT}/hook/permission`
4. Daemon creates a **pending approval** and forwards to hub
5. Hub broadcasts to PWA via WebSocket (same tile UI, just sourced differently)
6. Human clicks Allow / Deny / Always Allow in PWA
7. Hub responds to daemon's waiting request
8. Daemon responds to HTTP hook with `{ hookSpecificOutput: { decision: { behavior: "allow"|"deny" } } }`
9. Claude Code processes the decision and continues

**Timeout handling**:
- HTTP hooks have a configurable timeout (default 30s for HTTP, configurable per hook)
- Set `timeout: 120` in hook config for human approval window
- If timeout/error/non-2xx: **non-blocking error**, Claude Code falls back to terminal dialog
- This graceful degradation means the system works even without PWA connection

**Auto-approval policies** (A2A integration):
- For agent-to-agent missions, the daemon can auto-approve based on Agent Card policies
- No human in the loop needed for trusted cross-machine operations
- Policy engine already exists in `approval.py`, extended to cover tool permissions

**New daemon endpoint**:
```
POST /hook/permission
  Body: { session_id, tool_name, tool_input, permission_suggestions }
  Response: { hookSpecificOutput: { hookEventName: "PermissionRequest", decision: { behavior: "allow"|"deny" } } }
  Blocking: yes (waits for hub decision or timeout)
  On timeout/error: returns empty 200 (Claude Code shows terminal dialog)
```

**New hub endpoint**:
```
POST /api/attention/permission
  Body: { machine, session_id, tool_name, tool_input }
  Broadcasts: WebSocket event { type: "permission_request", ... }

POST /api/attention/permission/{request_id}/decide
  Body: { decision: "allow"|"deny" }
  Unblocks: the daemon's waiting HTTP response
```

### Layer 2: State Tracking (unchanged, already tmux-free)

The heartbeat system already works without tmux:
- `cc-heartbeat.sh` writes session state to `/tmp/cc-sessions/{pid}.json`
- `AttentionMonitor` polls these files
- States (working/thinking/waiting) derived from heartbeat age

No changes needed. This layer is already tmux-independent.

### Layer 3: Prompt Context Enrichment (replaces terminal capture)

For non-permission prompts (questions, text input, select menus), we enhance `notification_data`:

**Current notification_data** (from Notification hook):
- `notification_type`: permission_prompt, idle_prompt, question, ask_user
- `message`: human-readable text
- `title`: short title

**Enhanced notification_data** (from PermissionRequest + Notification hooks):
- Permission details come from Layer 1 (PermissionRequest hook) - much richer
- Questions/text prompts: keep using `notification_data` from Notification hook
- Add `tool_input` details when available

**Remaining gap**: SelectInput (arrow-key menus) lacks cursor position in notification_data. Mitigation: send numeric choice keys instead of arrow navigation. This is a minor UX degradation acceptable for eliminating tmux.

### Layer 4: Terminal View (deprecated, optional)

The terminal slide-up in the PWA (xterm.js rendering tmux capture) becomes **optional debug tooling**:

- Without tmux, no terminal view is available
- This is acceptable: the structured prompt overlay (permission buttons, question choices) provides all actionable information
- If a user wants terminal view, they can still use `claude-tmux.sh` wrapper — it becomes opt-in instead of required
- Long-term: Claude Code's `transcript_path` (JSONL) could provide a structured alternative to raw terminal capture

## Hook Configuration

### Installed by `install.sh` on each machine

File: `~/.claude/settings.local.json` (hooks section)

```json
{
  "hooks": {
    "PermissionRequest": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "http",
            "url": "http://localhost:7331/hook/permission",
            "timeout": 120
          }
        ]
      }
    ],
    "Notification": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/usr/local/bin/cc-heartbeat.sh notification"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/usr/local/bin/cc-heartbeat.sh start"
          }
        ]
      }
    ],
    "SessionStop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/usr/local/bin/cc-heartbeat.sh stop"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/usr/local/bin/cc-heartbeat.sh working"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "/usr/local/bin/cc-heartbeat.sh waiting"
          }
        ]
      }
    ]
  }
}
```

**Key changes**:
- `PermissionRequest` uses an **HTTP hook** (not command), enabling synchronous request/response with the daemon
- Uses correct `matcher` + `hooks` array format (Claude Code hook config spec)
- `timeout: 120` (seconds) gives human 2 minutes to respond; on timeout, Claude Code shows terminal dialog
- Multiple hooks per event supported (all run in parallel), so heartbeat + HTTP can coexist

## A2A Integration Points

This design maps directly onto the A2A protocol:

| A2A Concept | Implementation |
|------------|----------------|
| **Agent Card** | Declares `attention: true` capability + permission policies |
| **Task state** | Maps to attention states: `working` -> in_progress, `waiting` -> input_required |
| **SendMessage** | Human response to permission request = A2A message to agent task |
| **Task artifact** | Permission decision = structured task output |
| **Discovery** | `install.sh` auto-configures hooks = agent self-registration |

### Agent-to-Agent Permission Flow

When Agent A (serverlab) launches Agent B (limn) via a mission:
1. Agent B's PermissionRequest hook fires
2. Daemon forwards to hub with mission context
3. Hub checks Agent Card policies: "Agent A is trusted for Bash on limn" -> auto-approve
4. No human involvement needed
5. Fully automated cross-machine operation

This replaces the current `--dangerously-skip-permissions` workaround.

## Migration Path

### Phase 1: Add PermissionRequest HTTP Hook (daemon-side)

- New endpoint `POST /hook/permission` on daemon API
- Pending approval store (in-memory, timeout-based)
- Forward to hub via `POST /api/attention/permission`
- Hub broadcasts permission request via WebSocket
- PWA renders permission buttons (reuse existing tile UI)
- Hub returns decision, daemon unblocks hook response

**Result**: Permission prompts work without tmux. Terminal view and other prompts still use tmux if available.

### Phase 2: Promote notification_data as Primary

- Remove tmux as requirement in `install.sh`
- `_capture_terminal()` becomes optional (only if tmux detected)
- `notification_data` path becomes primary in `AttentionMonitor`
- PWA handles non-tmux sessions as first-class (not "monitor only")
- Remove "sub" badge for non-tmux sessions

**Result**: All machines work without tmux. `claude-tmux.sh` becomes optional.

### Phase 3: Clean Up tmux Code

- Deprecate `claude-tmux.sh` (keep for power users)
- Remove tmux requirement from BACKLOG.md B3
- Simplify `prompt_parser.py` (remove ANSI terminal parsing, keep notification_data parsing)
- Remove terminal polling from PWA (or make it opt-in)
- Update CLAUDE.md documentation

**Result**: Clean codebase, no tmux dependency in the main path.

### Phase 4: A2A Policy Engine

- Agent Cards define permission policies
- Hub auto-approves based on trust relationships
- Cross-machine missions run fully autonomously
- Human oversight via PWA becomes opt-in for trusted agent pairs

**Result**: Full A2A compliance. Agents negotiate permissions through protocol, not terminal.

## What We Lose (Acceptable Trade-offs)

| Lost Feature | Impact | Mitigation |
|-------------|--------|------------|
| Terminal slide-up view | Medium | Structured prompt overlay provides all actionable info |
| SelectInput arrow navigation | Low | Send numeric keys instead (1, 2, 3) |
| Real-time terminal polling | Low | Heartbeat state + notification_data sufficient |
| Full 30-line terminal context | Low | `transcript_path` JSONL for debug if needed |
| ANSI-colored command previews | Cosmetic | Plain text `tool_input` from hook |

## What We Gain

- **No MobaXterm conflicts** -- plain terminal, zero interference
- **Simpler install** -- no tmux dependency on target machines
- **Structured protocol** -- no ANSI parsing, no regex fragility
- **A2A-native** -- permission flow as protocol messages, not keystrokes
- **Auto-approval** -- agent-to-agent trust policies replace human clicking
- **Graceful degradation** -- falls back to terminal dialog if daemon offline
- **Faster response** -- HTTP hook is synchronous, no 3s polling delay

## Resolved Questions (validated 2026-03-06)

1. **HTTP hook timeout**: Default 30s for HTTP hooks, **configurable per hook** via `timeout` field (in seconds). Set to 120 for human approval. On timeout: non-blocking error, Claude Code shows terminal dialog. **RESOLVED: feasible.**

2. **Multiple hooks per event**: **Yes.** Multiple hooks in a matcher's `hooks` array run **in parallel**. Identical handlers deduplicated automatically. Command + HTTP hooks can coexist on the same event. **RESOLVED: no issue.**

3. **Question/text_input injection**: Accepted as Phase 1 limitation. Non-permission prompts (questions, text input) still require terminal typing. Rare in practice (~20% of interactions). Future: Claude Code may add response injection API. **RESOLVED: accepted trade-off.**

4. **Stop hook**: **Yes, exists and is reliable.** Provides `last_assistant_message` and `stop_hook_active` (boolean to prevent infinite loops). Does NOT fire on user interrupt. **RESOLVED: will use for state detection.**

5. **Hook error handling** (new): Non-2xx responses, connection failures, and timeouts are all **non-blocking errors** — Claude Code continues and shows terminal dialog. To actually block/deny, must return 2xx with JSON decision body. **RESOLVED: perfect graceful degradation.**
