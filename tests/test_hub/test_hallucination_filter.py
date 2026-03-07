"""Tests for STT hallucination filter."""

from src.hub.hallucination_filter import is_hallucination, normalize_for_comparison


def test_normal_text_passes():
    assert is_hallucination("Bonjour, comment ca va?") is None


def test_known_phrase_detected():
    result = is_hallucination("Sous-titrage ST")
    assert result is not None
    assert "known phrase" in result


def test_known_phrase_with_accents():
    result = is_hallucination("Merci d\u2019avoir regard\u00e9 cette vid\u00e9o")
    assert result is not None


def test_repetitive_text_detected():
    result = is_hallucination("bonjour bonjour bonjour bonjour")
    assert result is not None
    assert "repetitive" in result


def test_short_word_repetition_ignored():
    assert is_hallucination("le le le le") is None


def test_empty_text():
    assert is_hallucination("") is None
    assert is_hallucination("   ") is None


def test_normalize_curly_quotes():
    result = normalize_for_comparison("l\u2019homme")
    assert "'" in result


def test_normalize_strips_accents():
    result = normalize_for_comparison("\u00e9l\u00e8ve")
    assert result == "eleve"
