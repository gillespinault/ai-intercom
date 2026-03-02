#!/bin/bash
# cc-heartbeat.sh — Write/update a heartbeat file for the AttentionMonitor.
# Called by Claude Code hooks (SessionStart, Stop, Notification, UserPromptSubmit).
# Receives JSON on stdin from the hook system.
#
# Usage: echo '{"session_id":"...","cwd":"..."}' | cc-heartbeat.sh <action>
# Actions: start, stop, working, waiting

set -euo pipefail

ACTION="${1:-stop}"
INPUT=$(cat)

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')

PROJECT=$(basename "${CWD:-unknown}" 2>/dev/null || echo "unknown")

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

DIR="/tmp/cc-sessions"
mkdir -p "$DIR"

# PPID = ephemeral hook wrapper. The real Claude Code process is its parent.
CC_PID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
# Fallback to PPID if we can't resolve the grandparent
PID="${CC_PID:-$PPID}"
FILE="$DIR/${PID}.json"

case "$ACTION" in
    start|stop|working)
        TOOL_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
        ;;
    waiting)
        # Set a timestamp far in the past so idle_seconds >> 15s → WAITING
        TOOL_TIME="2000-01-01T00:00:00+00:00"
        ;;
    *)
        TOOL_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
        ;;
esac

# Write atomically via temp file
TMPFILE="$(mktemp "$DIR/.heartbeat.XXXXXX")"
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
  "rc_url": null
}
ENDJSON

mv "$TMPFILE" "$FILE"
