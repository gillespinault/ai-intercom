"""Terminal prompt parser for Claude Code output.

Parses raw tmux ``capture-pane`` output to detect what Claude Code is
currently showing the user: a permission prompt, a numbered-choice
question, or an idle text-input line.

Usage::

    from src.daemon.prompt_parser import parse_terminal_output

    prompt = parse_terminal_output(raw_pane_text)
    if prompt is not None:
        print(prompt.type, prompt.tool, prompt.choices)
"""

from __future__ import annotations

import json
import re

from src.shared.models import DetectedPrompt, PromptChoice, PromptType

# ---------------------------------------------------------------------------
# ANSI escape code stripper
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Permission detection
# ---------------------------------------------------------------------------

# Matches lines like:
#   "Claude wants to execute a Bash command"
#   "Claude wants to edit a file"
#   "Claude wants to write to a file"
#   "Claude wants to use the mcp__outline__search_documents tool"
_PERMISSION_RE = re.compile(
    r"Claude\s+wants\s+to\s+(?:execute\s+a\s+(\w+)\s+command"
    r"|edit\s+a\s+file"
    r"|write\s+to\s+a\s+file"
    r"|use\s+the\s+([\w.]+)\s+tool)",
    re.IGNORECASE,
)

# Extracts command or file path after "Command:" or "File:" labels.
_COMMAND_RE = re.compile(r"^\s*Command:\s*(.+)", re.MULTILINE)
_FILE_RE = re.compile(r"^\s*File:\s*(.+)", re.MULTILINE)
_ARGUMENTS_RE = re.compile(r"^\s*Arguments:\s*(.+)", re.MULTILINE)

# Detects the "Allow?" line that confirms this is a permission prompt.
_ALLOW_RE = re.compile(r"Allow\?", re.IGNORECASE)

# Extracts (key)label pairs from the Allow line, e.g. "(y)es", "(n)o"
_CHOICE_PAREN_RE = re.compile(r"\((\w)\)(\w*)")


def _try_permission(text: str) -> DetectedPrompt | None:
    perm_match = _PERMISSION_RE.search(text)
    if perm_match is None:
        return None

    # Must also have an "Allow?" line to confirm it's really a prompt.
    if not _ALLOW_RE.search(text):
        return None

    # Determine tool name.
    tool: str | None = None
    if perm_match.group(1):
        # "execute a Bash command" -> group(1) = "Bash"
        tool = perm_match.group(1)
    elif perm_match.group(2):
        # "use the mcp__outline__... tool" -> group(2) = tool name
        tool = perm_match.group(2)
    else:
        # "edit a file" / "write to a file"
        full = perm_match.group(0).lower()
        if "edit" in full:
            tool = "Edit"
        elif "write" in full:
            tool = "Write"

    # Extract command preview (command, file path, or arguments).
    command_preview: str | None = None
    cmd_match = _COMMAND_RE.search(text)
    file_match = _FILE_RE.search(text)
    args_match = _ARGUMENTS_RE.search(text)

    if cmd_match:
        # For multi-line commands, grab continuation lines.
        start = cmd_match.end()
        preview_lines = [cmd_match.group(1).strip()]
        for line in text[start:].splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("Allow"):
                preview_lines.append(stripped)
            else:
                break
        command_preview = " ".join(preview_lines)
    elif file_match:
        command_preview = file_match.group(1).strip()
    elif args_match:
        command_preview = args_match.group(1).strip()

    # Parse choices from the Allow? line.
    choices: list[PromptChoice] = []
    allow_line_match = _ALLOW_RE.search(text)
    if allow_line_match:
        # Find the line containing Allow?
        line_start = text.rfind("\n", 0, allow_line_match.start()) + 1
        line_end = text.find("\n", allow_line_match.start())
        if line_end == -1:
            line_end = len(text)
        allow_line = text[line_start:line_end]
        for m in _CHOICE_PAREN_RE.finditer(allow_line):
            key = m.group(1)
            label = key + m.group(2)  # e.g. "y" + "es" -> "yes"
            choices.append(PromptChoice(key=key, label=label))

    return DetectedPrompt(
        type=PromptType.PERMISSION,
        raw_text=text,
        tool=tool,
        command_preview=command_preview,
        choices=choices,
    )


# ---------------------------------------------------------------------------
# Question detection (numbered choices)
# ---------------------------------------------------------------------------

# Matches numbered option lines: "1. Option A", "  2. Option B"
_NUMBERED_OPTION_RE = re.compile(r"^\s*(\d+)\.\s+(.+)$", re.MULTILINE)

# The input prompt indicator at the end of the output.
_INPUT_PROMPT_RE = re.compile(r">\s*$")


def _try_question(text: str) -> DetectedPrompt | None:
    # Must end with a ">" prompt.
    if not _INPUT_PROMPT_RE.search(text):
        return None

    # Find numbered options.
    options = _NUMBERED_OPTION_RE.findall(text)
    if len(options) < 2:
        return None

    choices = [PromptChoice(key=num, label=label.strip()) for num, label in options]

    # Extract the question: look for a line ending with "?" that comes
    # before the first numbered option.
    first_option_match = _NUMBERED_OPTION_RE.search(text)
    question: str | None = None
    if first_option_match:
        preceding = text[: first_option_match.start()]
        # Find the last line with a question mark.
        for line in reversed(preceding.splitlines()):
            stripped = line.strip()
            if stripped and "?" in stripped:
                question = stripped
                break

    return DetectedPrompt(
        type=PromptType.QUESTION,
        raw_text=text,
        question=question,
        choices=choices,
        allows_free_text=False,
    )


# ---------------------------------------------------------------------------
# Text-input (idle) detection
# ---------------------------------------------------------------------------


def _try_text_input(text: str) -> DetectedPrompt | None:
    # Must end with a ">" prompt on its own (possibly with trailing space).
    # The ">" must be at the start of a line or be the entire content.
    lines = text.rstrip().splitlines()
    if not lines:
        return None

    last_line = lines[-1].strip()
    if last_line != ">":
        return None

    return DetectedPrompt(
        type=PromptType.TEXT_INPUT,
        raw_text=text,
        allows_free_text=True,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_terminal_output(raw: str) -> DetectedPrompt | None:
    """Parse tmux terminal output and detect Claude Code prompts.

    Args:
        raw: Raw text captured from ``tmux capture-pane -p``.

    Returns:
        A ``DetectedPrompt`` if a permission, question, or text-input
        prompt is detected, or ``None`` if Claude is still working.
    """
    if not raw:
        return None

    text = _strip_ansi(raw)

    # Try each detector in priority order.
    result = _try_permission(text)
    if result is not None:
        return result

    result = _try_question(text)
    if result is not None:
        return result

    result = _try_text_input(text)
    if result is not None:
        return result

    return None


# ---------------------------------------------------------------------------
# Notification data parser (fallback for non-tmux sessions)
# ---------------------------------------------------------------------------


def parse_notification_data(raw_json: str) -> DetectedPrompt | None:
    """Parse Claude Code hook Notification payload to extract prompt info.

    The Notification hook provides JSON on stdin with fields like::

        {
            "session_id": "...",
            "cwd": "...",
            "type": "permission_prompt",
            "tool": "Bash",
            "command": "ls -la",
            "message": "Claude wants to execute a Bash command",
            ...
        }

    This is used as a fallback when tmux capture is not available.

    Args:
        raw_json: Raw JSON string from the hook's stdin payload.

    Returns:
        A ``DetectedPrompt`` if the notification contains prompt info,
        or ``None`` if it cannot be parsed.
    """
    if not raw_json:
        return None

    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    notification_type = data.get("type", "")

    # Permission prompt from Notification hook
    if notification_type == "permission_prompt":
        tool = data.get("tool") or data.get("tool_name")
        command_preview = (
            data.get("command")
            or data.get("file_path")
            or data.get("arguments")
            or data.get("description")
        )
        # Truncate long previews
        if command_preview and len(command_preview) > 200:
            command_preview = command_preview[:200] + "..."

        choices = [
            PromptChoice(key="y", label="yes"),
            PromptChoice(key="n", label="no"),
        ]

        return DetectedPrompt(
            type=PromptType.PERMISSION,
            raw_text=data.get("message", "Permission requested"),
            tool=tool,
            command_preview=command_preview,
            choices=choices,
        )

    # Question / ask_user type notifications
    if notification_type in ("question", "ask_user", "user_question"):
        question_text = data.get("question") or data.get("message", "")
        options = data.get("options") or data.get("choices") or []
        choices = []
        for i, opt in enumerate(options):
            if isinstance(opt, dict):
                label = opt.get("label", opt.get("text", str(i + 1)))
                choices.append(PromptChoice(key=str(i + 1), label=label))
            elif isinstance(opt, str):
                choices.append(PromptChoice(key=str(i + 1), label=opt))

        return DetectedPrompt(
            type=PromptType.QUESTION,
            raw_text=question_text,
            question=question_text,
            choices=choices,
            allows_free_text=bool(data.get("allows_free_text", True)),
        )

    # Generic notification — extract what we can
    message = data.get("message", "")
    if message:
        return DetectedPrompt(
            type=PromptType.TEXT_INPUT,
            raw_text=message,
            allows_free_text=True,
        )

    return None
