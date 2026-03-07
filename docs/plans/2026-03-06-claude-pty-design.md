# Design: claude-pty — Transparent PTY Relay

**Date**: 2026-03-06
**Status**: Prototype
**Solves**: True redundant control (terminal + PWA) without tmux UX impact
**Complements**: PermissionRequest HTTP Hook (Phase 1)

## Problem

The user requires **true redundant control**: when Claude Code waits for input, either the terminal OR the PWA can respond — first responder wins. Two existing approaches were rejected:

1. **Blocking PermissionRequest hook**: Freezes terminal while waiting for PWA response. Unacceptable — terminal is the primary interface.
2. **tmux send-keys**: Impacts terminal experience (key bindings, scrollback, status bar, clipboard behavior). Not acceptable as the standard interaction path.

## Solution: claude-pty

A ~200-line Python script that wraps Claude Code in a transparent PTY pair. Zero visual impact — the user sees absolutely no difference from running `claude` directly.

```
Without claude-pty:      Terminal → Claude Code
With claude-pty:          Terminal → claude-pty → Claude Code
                                        |
                                   HTTP API (localhost)
                                        |
                              AttentionMonitor / Daemon
```

### What it provides

| Capability | tmux equivalent | claude-pty |
|-----------|----------------|------------|
| Terminal capture | `tmux capture-pane -p -S -30` | `GET http://localhost:{port}/capture` |
| Response injection | `tmux send-keys -t session keys Enter` | `POST http://localhost:{port}/inject` |
| Session wrapping | `tmux new-session -s cc-project claude` | `claude-pty [args...]` (transparent fork) |
| SelectInput nav | `tmux send-keys Down Down Enter` | `POST /inject {"keys": "select:2"}` |
| UX impact | Status bar, key bindings, scrollback altered | **None** |

### How it works

1. Creates a PTY pair (master/slave)
2. Forks: child runs `claude` on slave PTY, parent relays I/O
3. Starts HTTP server on random localhost port
4. Writes port to `/tmp/cc-sessions/pty-{child_pid}.port`
5. User types → bytes forwarded to child PTY (transparent)
6. Child output → forwarded to user terminal + buffered for `/capture`
7. SIGWINCH (resize) → forwarded to child
8. On exit: restores terminal, cleans up port file

### Port discovery

The port file uses the **Claude Code PID** (the child process), matching the PID used by `cc-heartbeat.sh` for heartbeat files. The attention monitor looks for `/tmp/cc-sessions/pty-{PID}.port` alongside `{PID}.json`.

## Redundant Control Flow

### For permission prompts (PermissionRequest hook + claude-pty)

```
1. Claude Code needs permission for Bash("docker ps")

2. PermissionRequest hook fires → POST localhost:7701/hook/permission
   Daemon: returns {} immediately (zero terminal impact)
   Daemon: sends async notification to hub with structured data

3. Terminal shows permission dialog: "Allow Bash: docker ps? (y/n)"

4. SIMULTANEOUSLY:
   Path A: User types 'y' in terminal → Claude Code continues
   Path B: User clicks "Allow" in PWA →
           Hub → Daemon → POST localhost:{pty_port}/inject {"keys": "y"}
           → PTY relay writes 'y\r' to master fd
           → Claude Code receives keystroke → continues

   FIRST RESPONDER WINS. If user already typed 'y', the injected 'y'
   arrives after Claude Code has moved on (harmless — just an extra
   character in the input buffer, consumed by the next prompt or ignored).
```

### For other prompts (questions, text input)

Same mechanism: attention monitor captures terminal via `/capture`, detects prompt, pushes to hub. PWA shows prompt with response options. User can respond from terminal or PWA.

### Auto-approval (agent-to-agent missions)

For headless missions, the PermissionRequest hook **blocks** and waits for the hub decision. No terminal dialog needed. The hook returns the decision directly.

## Integration Changes Required

### 1. cc-heartbeat.sh — Add PTY port discovery

```bash
# After PID detection, discover PTY port
PTY_PORT=""
PTY_PORT_FILE="$DIR/pty-${PID}.port"
[ -f "$PTY_PORT_FILE" ] && PTY_PORT=$(cat "$PTY_PORT_FILE")

# Add to heartbeat JSON
"pty_port": ${PTY_PORT:-null},
```

### 2. AttentionHeartbeat model — Add pty_port field

```python
class AttentionHeartbeat(BaseModel):
    # ... existing fields ...
    pty_port: int | None = None
```

### 3. AttentionMonitor — Use PTY relay when available

```python
def _capture_terminal(self, hb: AttentionHeartbeat) -> str | None:
    # Priority: PTY relay > tmux > None
    if hb.pty_port:
        return self._capture_via_pty(hb.pty_port)
    if hb.tmux_session:
        return self._capture_via_tmux(hb.tmux_session)
    return None

def _inject_response(self, hb: AttentionHeartbeat, keys: str) -> bool:
    if hb.pty_port:
        return self._inject_via_pty(hb.pty_port, keys)
    if hb.tmux_session:
        return self._inject_via_tmux(hb.tmux_session, keys)
    return False
```

### 4. Daemon respond endpoint — Accept PTY port

The `/api/attention/respond` endpoint needs to use PTY relay when available, falling back to tmux.

### 5. install.sh — Install claude-pty instead of claude-tmux

```bash
# Replace claude-tmux with claude-pty
cp scripts/claude-pty.py ~/.local/bin/claude-pty
chmod +x ~/.local/bin/claude-pty
alias claude='claude-pty'
```

## What This Does NOT Change

- Heartbeat file format (adds `pty_port`, backward compatible)
- Hub API (unchanged)
- PWA (unchanged — response flow is hub → daemon → inject, same as tmux)
- PermissionRequest hook endpoints (unchanged, complementary)
- Session state tracking (unchanged)

## Migration Path

1. **Phase 1** (done): PermissionRequest hook for structured notifications + auto-approval
2. **Phase 2** (this): claude-pty for transparent I/O relay, true redundant control
3. **Phase 3**: Remove tmux as default, make it opt-in for power users
4. **Phase 4**: Remove tmux code paths entirely

## Race Condition Analysis

**Q: What if user types AND PWA injects simultaneously?**

Both write to the same PTY master fd. The kernel serializes writes. One keystroke arrives first and gets processed by Claude Code. The second arrives after Claude Code has moved past the prompt. Depending on context:
- If Claude Code is now executing (no prompt): the extra keystroke goes into stdin buffer, harmlessly consumed later or ignored
- If Claude Code shows a new prompt: the keystroke might answer the wrong prompt — but this is the same race condition as a user accidentally pressing Enter twice in a terminal. It's a known, acceptable UX edge case.

**Mitigation**: The PWA should remove the permission tile as soon as it sends the inject request. The daemon's `_inject_response` should check if the session is still in WAITING state before injecting.

## Dependencies

- Python 3 standard library only (no external packages)
- No tmux, no pyte, no pexpect
- Works on any POSIX system with PTY support (Linux, macOS)
