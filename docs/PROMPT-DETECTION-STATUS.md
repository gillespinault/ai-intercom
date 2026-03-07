# Prompt Detection — Architecture

## Design

The terminal is the **sole source of truth** for prompt type detection.
Hooks only track session lifecycle and activity timing.

```
Claude Code hooks (SessionStart/Stop/Notification/UserPromptSubmit)
  → cc-heartbeat.sh writes JSON to /tmp/cc-sessions/<PID>.json
    → AttentionMonitor polls heartbeat files every 3s
      → idle_seconds from last_tool_time → state (WORKING/THINKING/WAITING)
      → When WAITING: capture terminal (PTY or tmux) → parse_terminal_output()
        → Permission (Allow?), Question (numbered/select), Text input (❯), or None
      → Push to Hub → WebSocket to PWA + Telegram notifications
```

## Hook actions

| Hook event | Action | Effect on heartbeat |
|------------|--------|---------------------|
| SessionStart | `start` | `last_tool_time = now` |
| SessionStop | `stop` | `last_tool_time = now` |
| UserPromptSubmit | `working` | `last_tool_time = now` |
| Notification | `activity` | Preserves existing `last_tool_time` (only touches file mtime for abandon detection) |

The `activity` action always preserves `last_tool_time` — notifications are
informational and should never reset the idle timer. Only `UserPromptSubmit`
(→ `working`) resets it, as the sole reliable signal that the user typed.
The file write still updates mtime, which is used for abandon detection.

## Why terminal-only

Previous architecture used `notification_data` from hooks to determine prompt type.
This was unreliable because:

1. **Auto-allowed permissions fire hooks too.** Claude Code sends `Notification(permission_prompt)`
   for every tool call, even auto-allowed ones. The stale `notification_data` persisted.
2. **Double-hook race condition.** Both `permission_prompt` matcher and catch-all fired,
   writing to the same file with different `last_tool_time` values.
3. **Hooks are events, not state.** A `permission_prompt` notification doesn't mean
   a permission is currently pending — it means one was requested at some point.

The terminal IS what the user sees. If it shows "Allow?", there's a real permission.
If it shows `❯`, Claude is waiting for input. If it shows neither, Claude is working.

## Scroll buffer protection

`_try_permission()` only searches the bottom 15 lines for "Allow?" to avoid
matching stale permission text in the scroll buffer.

## Terminal capture: claude-pty with pyte

`claude-pty` wraps Claude Code in a transparent PTY relay. Since 2026-03-06,
it uses the **pyte** terminal emulator library instead of regex-based ANSI
stripping for robust terminal capture.

| Aspect | Old (regex) | New (pyte) |
|--------|-------------|------------|
| ANSI stripping | `r"\x1b\[[0-9;]*[a-zA-Z]"` — missed DEC private mode (`\x1b[?2026h`) | Full VT100 emulation via `pyte.Screen` + `pyte.Stream` |
| Buffer | 200-line deque, last 30 lines returned | Full screen display (actual terminal dimensions) |
| Cursor positioning | Stripped as escape sequence, spacing lost | Properly rendered by virtual terminal |
| Output | Partial lines possible during active rendering | Complete screen state at any point |

The `/capture` endpoint returns `screen.display` — the complete rendered terminal
as the user sees it. This is fed directly to `parse_terminal_output()`.

## Known limitations

- Sessions without tmux/PTY show as WAITING with no prompt info (honest degradation).
- Real permissions take ~15s to appear (idle_seconds must grow past threshold).
- Long-running bash commands (>15s) show as WAITING with no prompt (correct — not a permission).
