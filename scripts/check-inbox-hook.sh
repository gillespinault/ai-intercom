#!/usr/bin/env bash
# Thin wrapper for Claude Code PostToolUse/UserPromptSubmit hooks
# Exits silently if no messages (< 5ms)
exec ai-intercom check-inbox --format hook 2>/dev/null
