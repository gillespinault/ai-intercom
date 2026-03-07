#!/bin/bash
# cc-heartbeat.sh — Write/update a heartbeat file for the AttentionMonitor.
# Called by Claude Code hooks (SessionStart, Stop, Notification, UserPromptSubmit).
# Receives JSON on stdin from the hook system.
#
# Usage: echo '{"session_id":"...","cwd":"..."}' | cc-heartbeat.sh <action>
# Actions: start, stop, working, activity

set -euo pipefail

ACTION="${1:-stop}"
INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')

# Detect project: walk up from CWD to find CLAUDE.md (preferred) or .git.
# In monorepos, subprojects have their own CLAUDE.md so we find those first.
# Special case: ~/.claude/skills/<name>/... → "skill:<name>"
detect_project() {
    local dir="${1:-unknown}"
    # Skill subprocess detection
    case "$dir" in
        */.claude/skills/*)
            echo "skill:$(echo "$dir" | sed 's|.*/.claude/skills/||' | cut -d/ -f1)"
            return ;;
    esac
    # Pass 1: detect projects/<name>/ convention (monorepo subprojects)
    # Must come first — prevents CLAUDE.md in the monorepo root from
    # swallowing subproject names.
    case "$dir" in
        */projects/*)
            echo "$dir" | sed 's|.*/projects/||' | cut -d/ -f1
            return ;;
    esac
    # Pass 2: look for CLAUDE.md (most specific project marker)
    local d="$dir"
    while [ "$d" != "/" ] && [ "$d" != "$HOME" ]; do
        if [ -f "$d/CLAUDE.md" ]; then
            basename "$d"
            return
        fi
        d=$(dirname "$d")
    done
    # Pass 3: fall back to .git root
    d="$dir"
    while [ "$d" != "/" ] && [ "$d" != "$HOME" ]; do
        if [ -d "$d/.git" ]; then
            basename "$d"
            return
        fi
        d=$(dirname "$d")
    done
    # Fallback to basename of original CWD
    basename "${1:-unknown}"
}
PROJECT=$(detect_project "${CWD:-unknown}")

# Machine ID from intercom config, fallback to hostname
MACHINE="$(hostname -s)"
if [ -f "$HOME/.config/ai-intercom/config.yml" ]; then
    MACHINE_FROM_CONFIG="$(grep -oP '^\s*id:\s*"\K[^"]+' "$HOME/.config/ai-intercom/config.yml" 2>/dev/null || true)"
    [ -n "$MACHINE_FROM_CONFIG" ] && MACHINE="$MACHINE_FROM_CONFIG"
fi

# Detect tmux session if running inside one
TMUX_SESSION=""
if [ -n "${TMUX:-}" ]; then
    TMUX_SESSION="$(tmux display-message -p '#S' 2>/dev/null || true)"
fi

# Detect claude-pty relay port (transparent PTY wrapper)
PTY_PORT=""

DIR="/tmp/cc-sessions"
mkdir -p "$DIR"

# PPID = ephemeral hook wrapper. The real Claude Code process is its parent.
CC_PID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
# Fallback to PPID if we can't resolve the grandparent
PID="${CC_PID:-$PPID}"
FILE="$DIR/${PID}.json"

# Discover claude-pty port file (written by claude-pty relay)
PTY_PORT_FILE="$DIR/pty-${PID}.port"
[ -f "$PTY_PORT_FILE" ] && PTY_PORT=$(cat "$PTY_PORT_FILE" 2>/dev/null || true)

# Extract transcript_path from hook payload or preserve from existing heartbeat
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // empty')
if [ -z "$TRANSCRIPT_PATH" ] && [ -f "$FILE" ]; then
    TRANSCRIPT_PATH=$(jq -r '.transcript_path // empty' "$FILE" 2>/dev/null || true)
fi

# Backward compatibility: normalize old action names
case "$ACTION" in
    waiting|notification) ACTION="activity" ;;
esac

case "$ACTION" in
    start|working|stop)
        TOOL_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
        ;;
    activity)
        # Notifications are informational — never reset idle timer.
        # Only UserPromptSubmit (→ working) should reset last_tool_time,
        # as it's the only reliable signal that the user typed something.
        # Preserving last_tool_time lets the session transition to WAITING
        # naturally when Claude finishes work and shows a prompt.
        # The file write still updates mtime (used for abandon detection).
        TOOL_TIME=""
        if [ -f "$FILE" ]; then
            TOOL_TIME=$(jq -r '.last_tool_time // empty' "$FILE" 2>/dev/null || echo "")
        fi
        [ -z "$TOOL_TIME" ] && TOOL_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
        ;;
    *)
        TOOL_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
        ;;
esac

# Write atomically via temp file
TMPFILE="$(mktemp "$DIR/.heartbeat.XXXXXX")"

# Format transcript_path as JSON value
if [ -n "$TRANSCRIPT_PATH" ]; then
    TP_JSON="\"$TRANSCRIPT_PATH\""
else
    TP_JSON="null"
fi

cat > "$TMPFILE" << ENDJSON
{
  "pid": $PID,
  "session_id": "$SESSION_ID",
  "session_name": "",
  "machine": "$MACHINE",
  "project": "$PROJECT",
  "last_tool": "hook-$ACTION",
  "last_tool_time": "$TOOL_TIME",
  "tmux_session": "$TMUX_SESSION",
  "pty_port": ${PTY_PORT:-null},
  "rc_url": null,
  "transcript_path": $TP_JSON
}
ENDJSON

mv "$TMPFILE" "$FILE"
