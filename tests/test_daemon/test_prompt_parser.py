"""Tests for the terminal prompt parser.

The parser analyzes raw tmux capture-pane output to detect what Claude Code
is currently displaying: permission prompts, questions, or idle text-input
states.
"""

import pytest

from src.daemon.prompt_parser import parse_terminal_output
from src.shared.models import PromptType


# ---------------------------------------------------------------------------
# Permission detection
# ---------------------------------------------------------------------------


class TestPermissionDetection:
    def test_bash_permission(self):
        raw = (
            "  Claude wants to execute a Bash command\n"
            "  Command: npm test\n"
            "  Allow? (y)es / (n)o / (a)lways allow for this session"
        )
        result = parse_terminal_output(raw)
        assert result is not None
        assert result.type == PromptType.PERMISSION
        assert result.tool == "Bash"
        assert "npm test" in (result.command_preview or "")
        assert len(result.choices) >= 2  # at least yes/no

    def test_edit_permission(self):
        raw = (
            "  Claude wants to edit a file\n"
            "  File: src/config.py\n"
            "  Allow? (y)es / (n)o / (a)lways allow for this session"
        )
        result = parse_terminal_output(raw)
        assert result is not None
        assert result.type == PromptType.PERMISSION
        assert result.tool == "Edit"
        assert "src/config.py" in (result.command_preview or "")

    def test_write_permission(self):
        raw = (
            "  Claude wants to write to a file\n"
            "  File: new.py\n"
            "  Allow? (y)es / (n)o / (a)lways allow for this session"
        )
        result = parse_terminal_output(raw)
        assert result is not None
        assert result.type == PromptType.PERMISSION
        assert result.tool == "Write"
        assert "new.py" in (result.command_preview or "")

    def test_mcp_tool_permission(self):
        raw = (
            "  Claude wants to use the mcp__outline__search_documents tool\n"
            "  Arguments: {\"query\": \"vacation\"}\n"
            "  Allow? (y)es / (n)o / (a)lways allow for this session"
        )
        result = parse_terminal_output(raw)
        assert result is not None
        assert result.type == PromptType.PERMISSION
        assert result.tool is not None
        assert "mcp__outline__search_documents" in result.tool

    def test_permission_with_multiline_command(self):
        raw = (
            "  Claude wants to execute a Bash command\n"
            "  Command: docker compose -f services/docker-compose.yml \\\n"
            "    up -d postgres\n"
            "  Allow? (y)es / (n)o / (a)lways allow for this session"
        )
        result = parse_terminal_output(raw)
        assert result is not None
        assert result.type == PromptType.PERMISSION
        assert result.tool == "Bash"
        assert "docker compose" in (result.command_preview or "")


# ---------------------------------------------------------------------------
# Question detection
# ---------------------------------------------------------------------------


class TestQuestionDetection:
    def test_numbered_options(self):
        raw = (
            "  Which approach do you prefer?\n"
            "  1. Option A\n"
            "  2. Option B\n"
            "  3. Option C\n"
            "  > "
        )
        result = parse_terminal_output(raw)
        assert result is not None
        assert result.type == PromptType.QUESTION
        assert result.question is not None
        assert "approach" in result.question.lower()
        assert len(result.choices) == 3

    def test_two_options(self):
        raw = (
            "  Proceed with the changes?\n"
            "  1. Yes\n"
            "  2. No\n"
            "  > "
        )
        result = parse_terminal_output(raw)
        assert result is not None
        assert result.type == PromptType.QUESTION
        assert len(result.choices) == 2

    def test_question_choices_have_labels(self):
        raw = (
            "  What would you like to do?\n"
            "  1. Create new file\n"
            "  2. Edit existing\n"
            "  > "
        )
        result = parse_terminal_output(raw)
        assert result is not None
        assert result.choices[0].key == "1"
        assert result.choices[0].label == "Create new file"
        assert result.choices[1].key == "2"
        assert result.choices[1].label == "Edit existing"

    def test_question_with_context_before(self):
        raw = (
            "  I found 3 files that match.\n"
            "  Which one should I edit?\n"
            "  1. src/main.py\n"
            "  2. src/utils.py\n"
            "  3. src/config.py\n"
            "  > "
        )
        result = parse_terminal_output(raw)
        assert result is not None
        assert result.type == PromptType.QUESTION
        assert "edit" in result.question.lower()
        assert len(result.choices) == 3


# ---------------------------------------------------------------------------
# Idle / text input detection
# ---------------------------------------------------------------------------


class TestIdleDetection:
    def test_text_input_prompt(self):
        raw = (
            "  I've completed the task. Is there anything else you need?\n"
            "  > "
        )
        result = parse_terminal_output(raw)
        assert result is not None
        assert result.type == PromptType.TEXT_INPUT
        assert result.allows_free_text is True

    def test_bare_prompt(self):
        raw = "> "
        result = parse_terminal_output(raw)
        assert result is not None
        assert result.type == PromptType.TEXT_INPUT

    def test_no_prompt_working(self):
        raw = (
            "  Reading file...\n"
            "  Analyzing code structure..."
        )
        result = parse_terminal_output(raw)
        assert result is None

    def test_spinner_working(self):
        raw = "  \u280b Thinking..."
        result = parse_terminal_output(raw)
        assert result is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_input(self):
        result = parse_terminal_output("")
        assert result is None

    def test_whitespace_only(self):
        result = parse_terminal_output("   \n  \n  ")
        assert result is None

    def test_ansi_codes_stripped(self):
        # Bold + color escape codes wrapping a permission prompt
        raw = (
            "\x1b[1m\x1b[33m  Claude wants to execute a Bash command\x1b[0m\n"
            "  Command: ls -la\n"
            "  Allow? (y)es / (n)o / (a)lways allow for this session"
        )
        result = parse_terminal_output(raw)
        assert result is not None
        assert result.type == PromptType.PERMISSION
        assert result.tool == "Bash"
        assert "ls -la" in (result.command_preview or "")

    def test_garbage_text(self):
        result = parse_terminal_output("xyzzy foo bar baz 12345")
        assert result is None

    def test_none_input(self):
        """parse_terminal_output should handle None gracefully."""
        result = parse_terminal_output(None)  # type: ignore[arg-type]
        assert result is None

    def test_raw_text_preserved(self):
        raw = (
            "  Claude wants to execute a Bash command\n"
            "  Command: echo hello\n"
            "  Allow? (y)es / (n)o / (a)lways allow for this session"
        )
        result = parse_terminal_output(raw)
        assert result is not None
        assert result.raw_text  # should contain original (cleaned) text

    def test_prompt_at_end_not_middle(self):
        """A '>' in the middle of text is not a prompt."""
        raw = (
            "  Output > 5 means the test passed.\n"
            "  Continuing analysis..."
        )
        result = parse_terminal_output(raw)
        assert result is None
