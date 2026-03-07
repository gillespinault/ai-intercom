"""Telegram message formatting helpers (no telegram dependency)."""

from __future__ import annotations

import re


def _sanitize_markdown_v1(text: str) -> str:
    """Sanitize text for Telegram MarkdownV1 compatibility.

    - Converts **bold** (V2) to *bold* (V1)
    - Escapes unmatched _ characters
    """
    # Convert **bold** to *bold*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)

    # Escape unmatched underscores (not inside _italic_ pairs)
    # Count underscores — if odd number of parts means even underscores (paired)
    parts = text.split('_')
    if len(parts) % 2 == 0:
        # Odd number of underscores — escape standalone ones
        text = re.sub(r'(?<=\w)_(?=\w)', r'\\_', text)

    return text


def _split_message(text: str, max_len: int = 4000) -> list[str]:
    """Split text into chunks that fit Telegram's message limit.

    Splits on paragraph boundaries (\\n\\n), then newlines, then hard cut.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    parts: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            parts.append(remaining)
            break

        # Try paragraph boundary
        cut = remaining.rfind("\n\n", 0, max_len)
        if cut > 0:
            parts.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip("\n")
            continue

        # Try newline
        cut = remaining.rfind("\n", 0, max_len)
        if cut > 0:
            parts.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip("\n")
            continue

        # Hard cut
        parts.append(remaining[:max_len])
        remaining = remaining[max_len:]

    return parts
