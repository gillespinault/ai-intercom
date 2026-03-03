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

# Detect project: walk up from CWD to find first dir with CLAUDE.md or .git.
# Special case: ~/.claude/skills/<name>/... → "skill:<name>"
detect_project() {
    local dir="${1:-unknown}"
    # Skill subprocess detection
    case "$dir" in
        */.claude/skills/*)
            echo "skill:$(echo "$dir" | sed 's|.*/.claude/skills/||' | cut -d/ -f1)"
            return ;;
    esac
    # Walk up to find project root
    while [ "$dir" != "/" ] && [ "$dir" != "$HOME" ]; do
        if [ -f "$dir/CLAUDE.md" ] || [ -d "$dir/.git" ]; then
            basename "$dir"
            return
        fi
        dir=$(dirname "$dir")
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

DIR="/tmp/cc-sessions"
mkdir -p "$DIR"

# PPID = ephemeral hook wrapper. The real Claude Code process is its parent.
CC_PID=$(ps -o ppid= -p $PPID 2>/dev/null | tr -d ' ')
# Fallback to PPID if we can't resolve the grandparent
PID="${CC_PID:-$PPID}"
FILE="$DIR/${PID}.json"

# Capture notification payload (truncated to 2000 chars) for non-tmux context
NOTIFICATION_DATA=""
case "$ACTION" in
    start|stop|working)
        TOOL_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
        # Preserve notification_data from existing heartbeat (B2 fix)
        if [ -f "$FILE" ]; then
            NOTIFICATION_DATA=$(jq '.notification_data // ""' "$FILE" 2>/dev/null || echo '""')
        fi
        ;;
    waiting)
        # Set a timestamp 60s in the past so idle_seconds >> 15s → WAITING
        # (avoids sentinel year-2000 values that produce absurd idle_seconds)
        TOOL_TIME=$(date -u -d '60 seconds ago' +"%Y-%m-%dT%H:%M:%S+00:00" 2>/dev/null || date -u +"%Y-%m-%dT%H:%M:%S+00:00")
        # Capture the hook payload as notification context
        NOTIFICATION_DATA=$(echo "$INPUT" | head -c 2000 | jq -Rs '.')
        ;;
    notification)
        # Store notification context without changing last_tool_time.
        # Read existing heartbeat to preserve last_tool_time.
        if [ -f "$FILE" ]; then
            TOOL_TIME=$(jq -r '.last_tool_time // empty' "$FILE" 2>/dev/null || echo "")
        fi
        [ -z "$TOOL_TIME" ] && TOOL_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
        NOTIFICATION_DATA=$(echo "$INPUT" | head -c 2000 | jq -Rs '.')
        ACTION="notification"
        ;;
    *)
        TOOL_TIME=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
        ;;
esac

# Write atomically via temp file
TMPFILE="$(mktemp "$DIR/.heartbeat.XXXXXX")"
# Default NOTIFICATION_DATA to empty JSON string if not set
[ -z "$NOTIFICATION_DATA" ] && NOTIFICATION_DATA='""'

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
  "rc_url": null,
  "notification_data": $NOTIFICATION_DATA
}
ENDJSON

mv "$TMPFILE" "$FILE"
