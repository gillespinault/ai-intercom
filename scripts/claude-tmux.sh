#!/bin/bash
# claude-tmux — Launch Claude Code inside a tmux session for full Attention Hub support.
#
# When Claude Code runs inside tmux, the Attention Hub can:
#   1. Capture terminal output via `tmux capture-pane`
#   2. Detect prompts (permissions, questions, text input)
#   3. Inject responses from the PWA via `tmux send-keys`
#
# Without tmux, sessions are "monitor only" — state is visible but
# responses cannot be sent from the Attention Hub.
#
# Usage:
#   claude-tmux [claude args...]
#
# Session naming:
#   cc-<project-dir>     (e.g. cc-AI-intercom)
#   cc-<project-dir>-N   (if name already taken)
#
# To make this the default, add to ~/.bashrc or ~/.zshrc:
#   alias claude='claude-tmux'
#   — or —
#   source <(ai-intercom shell-init)  # future enhancement
#
# Requirements: tmux, claude

set -euo pipefail

# If already inside tmux, just run claude directly — no double wrapping.
if [ -n "${TMUX:-}" ]; then
    exec claude "$@"
fi

# If tmux is not installed, fall back to plain claude with a warning.
if ! command -v tmux &>/dev/null; then
    echo "[claude-tmux] tmux not installed. Running without attention monitoring." >&2
    exec claude "$@"
fi

# If claude is not installed, bail out.
if ! command -v claude &>/dev/null; then
    echo "[claude-tmux] Error: 'claude' command not found." >&2
    exit 1
fi

# Generate session name from current directory.
PROJECT=$(basename "$(pwd)")
SESSION="cc-${PROJECT}"

# If a session with this name already exists, append a suffix.
if tmux has-session -t "$SESSION" 2>/dev/null; then
    SUFFIX=2
    while tmux has-session -t "${SESSION}-${SUFFIX}" 2>/dev/null; do
        SUFFIX=$((SUFFIX + 1))
    done
    SESSION="${SESSION}-${SUFFIX}"
fi

# Build the command string with proper shell escaping for tmux.
CMD="claude"
for arg in "$@"; do
    CMD="$CMD $(printf '%q' "$arg")"
done

# Launch a new tmux session running claude.
# When claude exits, the tmux session closes automatically.
exec tmux new-session -s "$SESSION" "$CMD"
