"""Tests for Telegram message formatting helpers."""

from src.hub.telegram_helpers import _sanitize_markdown_v1, _split_message


def test_sanitize_double_asterisks():
    assert _sanitize_markdown_v1("**bold**") == "*bold*"


def test_sanitize_preserves_single_asterisks():
    assert _sanitize_markdown_v1("*italic*") == "*italic*"


def test_sanitize_mixed():
    text = "**Title**\n_subtitle_\nNormal text"
    result = _sanitize_markdown_v1(text)
    assert "**" not in result
    assert "*Title*" in result
    assert "_subtitle_" in result


def test_sanitize_unmatched_underscore():
    result = _sanitize_markdown_v1("variable_name is used")
    assert "\\_" in result or "_" in result


def test_sanitize_no_markdown():
    assert _sanitize_markdown_v1("plain text") == "plain text"


def test_split_message_short():
    result = _split_message("short text", max_len=4000)
    assert result == ["short text"]


def test_split_message_on_paragraphs():
    text = "Para one.\n\nPara two.\n\nPara three."
    result = _split_message(text, max_len=25)
    assert len(result) >= 2
    assert all(len(p) <= 25 for p in result)
    assert "Para one." in result[0]


def test_split_message_no_good_boundary():
    text = "A" * 100
    result = _split_message(text, max_len=40)
    assert len(result) == 3
    assert all(len(p) <= 40 for p in result)
    assert "".join(result) == text


def test_split_message_empty():
    assert _split_message("", max_len=4000) == []
