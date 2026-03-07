"""STT hallucination filter.

Detects common Whisper artifacts (training-data phrases, repetitive
noise transcriptions). Adapted from mnemos hallucination_filter.
"""

from __future__ import annotations

import re
import unicodedata

# Known hallucination phrases — matched as SUBSTRINGS (not exact).
# All normalized: lowercase, no accents, straight quotes.
_HALLUCINATION_PHRASES = (
    "sous-titrage st",
    "sous-titres realises par",
    "merci d'avoir regarde",
    "thanks for watching",
    "thank you for watching",
)

# Regex for repetitive text: same word (3+ chars) repeated 3+ times.
_REPETITION_RE = re.compile(
    r"\b(\w{3,})\b(?:[\W\w]*?\b\1\b){2,}",
    re.IGNORECASE,
)


def normalize_for_comparison(text: str) -> str:
    """Normalize text for hallucination comparison.

    Lowercase, curly quotes -> straight, strip accents, strip punctuation.
    """
    text = text.lower()
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.strip().rstrip(".!?,;:").strip()
    return text


def is_hallucination(text: str) -> str | None:
    """Detect STT hallucinations. Returns reason string or None if OK.

    Checks:
    1. Known hallucination phrases (substring match)
    2. Repetitive text (same word 3+ times)
    """
    if not text or not text.strip():
        return None

    normalized = normalize_for_comparison(text)

    # 1. Known hallucination phrases (substring match)
    for phrase in _HALLUCINATION_PHRASES:
        if phrase in normalized:
            return f"known phrase: '{phrase}'"

    # 2. Repetitive text
    if _REPETITION_RE.search(normalized):
        return f"repetitive text: '{text.strip()[:40]}'"

    return None
