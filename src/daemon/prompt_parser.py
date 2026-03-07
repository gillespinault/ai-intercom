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
#   "Claude needs your permission to use Bash"  (Claude Code 2.x)
_PERMISSION_RE = re.compile(
    r"Claude\s+(?:wants\s+to|needs\s+your\s+permission\s+to)"
    r"\s+(?:execute\s+a\s+(\w+)\s+command"
    r"|edit\s+a\s+file"
    r"|write\s+to\s+a\s+file"
    r"|use\s+(?:the\s+)?([\w.]+)(?:\s+tool)?)",
    re.IGNORECASE,
)

# Extracts command or file path after "Command:" or "File:" labels.
_COMMAND_RE = re.compile(r"^\s*Command:\s*(.+)", re.MULTILINE)
_FILE_RE = re.compile(r"^\s*File:\s*(.+)", re.MULTILINE)
_ARGUMENTS_RE = re.compile(r"^\s*Arguments:\s*(.+)", re.MULTILINE)

# Extracts tool invocation from the modern format: ● Bash(command here)
# or ● Edit(/path/to/file) or ● Write(/path) etc.
_TOOL_INVOCATION_RE = re.compile(
    r"[●]\s*(\w+)\((.+?)(?:\)?\s*$)", re.MULTILINE
)

# Detects the "Allow?" line that confirms this is a permission prompt.
_ALLOW_RE = re.compile(r"Allow\?", re.IGNORECASE)

# Extracts (key)label pairs from the Allow line, e.g. "(y)es", "(n)o"
_CHOICE_PAREN_RE = re.compile(r"\((\w)\)(\w*)")

# Modern Claude Code permission menu format:
#   Allow?
#   ❯ Yes
#     Yes, and don't ask again for Bash
#     No, and tell Claude what to do instead
_MENU_OPTION_RE = re.compile(
    r"^\s*(?:[❯>\u276f]\s*)?(.+)$"
)


def _try_permission(text: str) -> DetectedPrompt | None:
    # "Allow?" must appear in the bottom 15 lines to be a current prompt.
    # Stale permission text in the scroll buffer must not match.
    lines = text.splitlines()
    bottom_text = "\n".join(lines[-15:]) if len(lines) > 15 else text
    if not _ALLOW_RE.search(bottom_text):
        return None

    perm_match = _PERMISSION_RE.search(text)
    if perm_match is None:
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

    # Fallback: extract from modern ● ToolName(command) format.
    if not command_preview:
        invocation = _TOOL_INVOCATION_RE.search(text)
        if invocation:
            invoked_tool = invocation.group(1)
            command_preview = invocation.group(2).strip()
            # Also set tool from invocation if not already set.
            if not tool:
                tool = invoked_tool

    # Parse choices from the Allow? line or the menu lines after it.
    choices: list[PromptChoice] = []
    allow_line_match = _ALLOW_RE.search(text)
    if allow_line_match:
        # Find the line containing Allow?
        line_start = text.rfind("\n", 0, allow_line_match.start()) + 1
        line_end = text.find("\n", allow_line_match.start())
        if line_end == -1:
            line_end = len(text)
        allow_line = text[line_start:line_end]

        # Try legacy format first: (y)es | (n)o | (a)lways
        for m in _CHOICE_PAREN_RE.finditer(allow_line):
            key = m.group(1)
            label = key + m.group(2)  # e.g. "y" + "es" -> "yes"
            choices.append(PromptChoice(key=key, label=label))

        # If no legacy choices found, try modern menu format:
        # Lines after "Allow?" that look like menu options
        if not choices:
            after_allow = text[line_end:]
            choices = _parse_menu_choices(after_allow)

    return DetectedPrompt(
        type=PromptType.PERMISSION,
        raw_text=text,
        tool=tool,
        command_preview=command_preview,
        choices=choices or [
            PromptChoice(key="y", label="Yes"),
            PromptChoice(key="n", label="No"),
        ],
    )


def _parse_menu_choices(text_after_allow: str) -> list[PromptChoice]:
    """Parse modern Claude Code menu-style choices.

    Modern format (lines after ``Allow?``)::

        ❯ Yes
          Yes, and don't ask again for Bash
          No, and tell Claude what to do instead

    Maps recognised labels to shortcut keys that Claude Code accepts.
    """
    # Key mappings for known menu labels
    _KEY_MAP = [
        ("y", re.compile(r"^yes$", re.IGNORECASE)),
        ("a", re.compile(r"^yes,?\s+and\s+don.?t\s+ask\s+again", re.IGNORECASE)),
        ("n", re.compile(r"^no", re.IGNORECASE)),
    ]

    choices: list[PromptChoice] = []
    for line in text_after_allow.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Remove leading ❯ or > indicator
        cleaned = re.sub(r"^[❯>\u276f]\s*", "", stripped).strip()
        if not cleaned:
            continue
        # Skip decorator lines (separators, status bar, etc.)
        if re.match(r"^[─━═\-]+$", cleaned):
            break
        if re.match(r"^[🤖⏵✻●]", cleaned):
            break

        # Match against known labels
        key = str(len(choices) + 1)  # fallback: numbered
        for k, pattern in _KEY_MAP:
            if pattern.match(cleaned):
                key = k
                break

        choices.append(PromptChoice(key=key, label=cleaned))

        # Stop after collecting reasonable number of choices
        if len(choices) >= 5:
            break

    return choices


# ---------------------------------------------------------------------------
# Question detection (numbered choices)
# ---------------------------------------------------------------------------

# Matches numbered option lines: "1. Option A", "  2. Option B"
_NUMBERED_OPTION_RE = re.compile(r"^\s*(\d+)\.\s+(.+)$", re.MULTILINE)

# The input prompt indicator at the end of the output (> or ❯).
_INPUT_PROMPT_RE = re.compile(r"[>\u276f]\s*$")


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
# SelectInput detection (Claude Code arrow-navigable menus)
# ---------------------------------------------------------------------------

# Matches SelectInput option lines:
#   "❯ 1. Yes"    (selected, with ❯ indicator)
#   "  2. No"     (unselected)
# Also handles lines without numbering: "❯ Yes", "  No"
_SELECT_OPTION_RE = re.compile(
    r"^\s*(?:[❯>\u276f]\s+)?(\d+)\.\s+(.+)$", re.MULTILINE
)

# Hint line at the bottom of SelectInput prompts.
_SELECT_HINT_RE = re.compile(
    r"Esc\s+to\s+cancel|Tab\s+to\s+amend|ctrl\+e\s+to\s+explain",
    re.IGNORECASE,
)

# Selected option indicator: line starting with ❯ followed by content.
_SELECT_INDICATOR_RE = re.compile(r"^\s*[❯>\u276f]\s+\d+\.", re.MULTILINE)


def _try_select_input(text: str) -> DetectedPrompt | None:
    """Detect Claude Code's SelectInput widget.

    Modern Claude Code prompts (e.g. ``Do you want to proceed?``)
    use an interactive SelectInput that renders as::

        Do you want to proceed?
        ❯ 1. Yes
          2. No

        Esc to cancel · Tab to amend · ctrl+e to explain

    The ``❯`` indicates the currently focused option.  Arrow keys
    navigate between options, Enter confirms.
    """
    # Must have at least one line with the ❯ indicator on a numbered option.
    # Only search the bottom portion of the terminal to avoid matching
    # historical SelectInput widgets in the scroll buffer.
    lines = text.splitlines()
    bottom_cutoff = max(0, len(lines) - 30)
    bottom_text = "\n".join(lines[bottom_cutoff:])
    indicator_match = _SELECT_INDICATOR_RE.search(bottom_text)
    if not indicator_match:
        return None

    # Collect only the contiguous block of numbered options around the
    # ❯ indicator.  The terminal buffer may contain old numbered lists
    # from history that must NOT be included.
    indicator_lineno = bottom_cutoff + bottom_text[:indicator_match.start()].count("\n")

    # Walk up from indicator to find contiguous options above.
    block_start = indicator_lineno
    for i in range(indicator_lineno - 1, -1, -1):
        if _SELECT_OPTION_RE.match(lines[i]):
            block_start = i
        else:
            break

    # Walk down from indicator to find contiguous options below.
    block_end = indicator_lineno
    for i in range(indicator_lineno + 1, len(lines)):
        if _SELECT_OPTION_RE.match(lines[i]):
            block_end = i
        else:
            break

    # Extract options only from the contiguous block.
    block_text = "\n".join(lines[block_start : block_end + 1])
    options = _SELECT_OPTION_RE.findall(block_text)
    if len(options) < 2:
        return None

    # Determine which option is currently selected (has ❯ prefix).
    selected_index = 0
    for line in block_text.splitlines():
        stripped = line.strip()
        m = re.match(r"^[❯>\u276f]\s+(\d+)\.", stripped)
        if m:
            sel_num = m.group(1)
            for idx, (num, _label) in enumerate(options):
                if num == sel_num:
                    selected_index = idx
                    break
            break

    # Build choices.  The key encodes the position offset from the
    # currently selected option so the injection logic knows how
    # many Down presses to send.
    choices: list[PromptChoice] = []
    for i, (num, label) in enumerate(options):
        offset = i - selected_index  # 0 = already selected, +1 = one Down, etc.
        key = f"select:{offset}"
        choices.append(PromptChoice(key=key, label=label.strip()))

    # Extract question text: look for a line with "?" above the option block.
    question: str | None = None
    for i in range(block_start - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped and "?" in stripped:
            question = stripped
            break
        if stripped:
            break  # Non-empty non-question line — stop looking.

    # Extract context: first try ● ToolName(command) pattern, then
    # fall back to description text above question.
    command_preview: str | None = None
    tool: str | None = None
    invocation = _TOOL_INVOCATION_RE.search(text)
    if invocation:
        tool = invocation.group(1)
        command_preview = invocation.group(2).strip()
        if len(command_preview) > 120:
            command_preview = command_preview[:117] + "\u2026"

    if not command_preview:
        for line in reversed(lines[:block_start]):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped == question:
                continue
            if "?" not in stripped and not re.match(r"^[❯>\u276f─━═\-]", stripped):
                command_preview = stripped
                if len(command_preview) > 120:
                    command_preview = command_preview[:117] + "\u2026"
                break

    return DetectedPrompt(
        type=PromptType.QUESTION,
        raw_text=text,
        question=question,
        tool=tool,
        command_preview=command_preview,
        choices=choices,
        allows_free_text=False,
    )


# ---------------------------------------------------------------------------
# Text-input (idle) detection
# ---------------------------------------------------------------------------


def _try_text_input(text: str) -> DetectedPrompt | None:
    # Detect the Claude Code idle input prompt.
    # The terminal typically ends with:
    #   ❯                       (or >)
    #   ──────────────────────  (separator line)
    #   esc to interrupt        (hint)
    #
    # We scan from the bottom, skipping decorator lines (separators,
    # hints, blank lines), looking for a prompt character.
    lines = text.rstrip().splitlines()
    if not lines:
        return None

    # Decorator patterns to skip when scanning from the bottom.
    _DECORATOR_RE = re.compile(
        r"^[\s─━═\-─]*$"          # separator lines (box-drawing chars)
        r"|^\s*esc\s+to\s+"       # "esc to interrupt" hint
        r"|^\s*Tip:\s+"           # "Tip: ..." suggestions
        r"|^\s*\?\s+for\s+"       # "? for shortcuts" hint
        r"|^\s*\u23f5"            # ⏵ accept edits / fast-forward hints
        r"|^\s*\U0001f916"        # 🤖 ccusage statusline
        r"|^\s*\U0001f4b0"        # 💰 ccusage cost line
        r"|^\s*\U0001f525"        # 🔥 context warning line
    )

    prompt_line = None
    for line in reversed(lines):
        # Normalise non-breaking spaces (\xa0) and other Unicode whitespace
        stripped = line.replace("\xa0", " ").strip()
        if not stripped:
            continue
        if _DECORATOR_RE.match(stripped):
            continue
        prompt_line = stripped
        break

    if prompt_line is None:
        return None

    # Accept both ">" (legacy) and "❯" (Claude Code 2.x) as prompt indicators.
    # The prompt line may contain hint text after the symbol, e.g.
    # "❯ Press up to edit queued messages" when the user is typing.
    if prompt_line in (">",) or prompt_line.startswith("\u276f"):
        return DetectedPrompt(
            type=PromptType.TEXT_INPUT,
            raw_text=text,
            allows_free_text=True,
        )

    return None


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
    # Note: the prompt TYPE is now determined by hook state in the
    # attention monitor, not here.  This parser is used for enrichment
    # (command_preview, choices, select menus).  The order below
    # optimises for the most specific match.
    result = _try_permission(text)
    if result is None:
        result = _try_select_input(text)
    if result is None:
        result = _try_question(text)
    if result is None:
        result = _try_text_input(text)

    # Truncate raw_text to avoid sending full terminal capture over WebSocket.
    if result is not None and len(result.raw_text) > 300:
        result.raw_text = result.raw_text[:300] + "\u2026"

    return result


# ---------------------------------------------------------------------------
# Notification data parser (fallback for non-tmux sessions)
# ---------------------------------------------------------------------------


def parse_notification_data(raw_json: str) -> DetectedPrompt | None:
    """Parse Claude Code hook Notification payload to extract prompt info.

    Claude Code sends JSON on stdin with ``notification_type`` as the key
    discriminator.  Real-world payloads look like::

        {
            "session_id": "...",
            "cwd": "...",
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
            "message": "Claude wants to execute a Bash command",
            ...
        }

    For backwards compatibility, the legacy ``type`` field is also checked.

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

    # Claude Code uses "notification_type"; legacy/test data may use "type"
    notification_type = data.get("notification_type") or data.get("type", "")

    # Permission prompt from Notification hook
    if notification_type == "permission_prompt":
        tool = data.get("tool_name") or data.get("tool")
        # Claude Code doesn't always send tool_name — extract from message text.
        if not tool:
            message = data.get("message", "")
            perm_match = _PERMISSION_RE.search(message)
            if perm_match:
                tool = perm_match.group(1) or perm_match.group(2)
                if not tool:
                    full = perm_match.group(0).lower()
                    if "edit" in full:
                        tool = "Edit"
                    elif "write" in full:
                        tool = "Write"
        command_preview = (
            data.get("command")
            or data.get("file_path")
            or data.get("arguments")
            or data.get("description")
        )
        # Try tool_input dict (some Claude Code versions nest details here)
        if not command_preview and isinstance(data.get("tool_input"), dict):
            ti = data["tool_input"]
            command_preview = (
                ti.get("command") or ti.get("file_path")
                or ti.get("pattern") or ti.get("query")
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

    # Idle prompt — Claude is waiting for free-text user input
    if notification_type == "idle_prompt":
        return DetectedPrompt(
            type=PromptType.TEXT_INPUT,
            raw_text=data.get("message", "Waiting for input"),
            allows_free_text=True,
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
