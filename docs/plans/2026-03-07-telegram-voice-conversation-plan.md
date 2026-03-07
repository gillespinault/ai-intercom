# Telegram Voice Conversation Pipeline — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix STT truncation, make Telegram responses visible, add supervised conversation mode, PWA dispatcher preferences, and per-agent voice synthesis.

**Architecture:** The hub's voice pipeline gets chunked STT (25s segments with initial_prompt chaining) and sentence-based TTS. The dispatcher gains conversation state tracking (ActiveConversationManager) to support message injection into active missions. The PWA gets a new "Dispatcher" prefs section that controls conversation behavior. Agent voice styles allow distinct TTS voices for inter-agent message synthesis.

**Tech Stack:** Python 3.12, FastAPI, httpx, python-telegram-bot, SQLite, vanilla JS (PWA)

**Design doc:** `docs/plans/2026-03-07-telegram-voice-conversation-design.md`

---

## Task 1: Hallucination Filter

**Files:**
- Create: `src/hub/hallucination_filter.py`
- Test: `tests/test_hub/test_hallucination_filter.py`

**Step 1: Write the failing tests**

```python
# tests/test_hub/test_hallucination_filter.py
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
    # Words < 3 chars should not trigger
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
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hub/test_hallucination_filter.py -v`
Expected: FAIL with "ModuleNotFoundError"

**Step 3: Implement the module**

Copy from mnemos with no changes — the module is self-contained (67 lines):

```python
# src/hub/hallucination_filter.py
"""Shared STT hallucination filter.

Detects common STT hallucinations (training-data artifacts, repetitive
noise transcriptions). Adapted from mnemos/stt/hallucination_filter.py.
"""

from __future__ import annotations

import re
import unicodedata

_HALLUCINATION_PHRASES = (
    "sous-titrage st",
    "sous-titres realises par",
    "merci d'avoir regarde",
    "thanks for watching",
    "thank you for watching",
)

_REPETITION_RE = re.compile(
    r"\b(\w{3,})\b(?:[\W\w]*?\b\1\b){2,}",
    re.IGNORECASE,
)


def normalize_for_comparison(text: str) -> str:
    """Normalize text for hallucination comparison."""
    text = text.lower()
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.strip().rstrip(".!?,;:").strip()
    return text


def is_hallucination(text: str) -> str | None:
    """Detect STT hallucinations. Returns reason string or None if OK."""
    if not text or not text.strip():
        return None

    normalized = normalize_for_comparison(text)

    for phrase in _HALLUCINATION_PHRASES:
        if phrase in normalized:
            return f"known phrase: '{phrase}'"

    if _REPETITION_RE.search(normalized):
        return f"repetitive text: '{text.strip()[:40]}'"

    return None
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hub/test_hallucination_filter.py -v`
Expected: all 8 PASS

**Step 5: Commit**

```bash
git add src/hub/hallucination_filter.py tests/test_hub/test_hallucination_filter.py
git commit -m "feat(stt): add hallucination filter adapted from mnemos"
```

---

## Task 2: STT Chunked Transcription

**Files:**
- Modify: `src/hub/voice_services.py` (replace `transcribe()`)
- Modify: `tests/test_hub/test_voice_services.py` (add chunking tests)

**Step 1: Write the failing tests**

Append to `tests/test_hub/test_voice_services.py`:

```python
# --- chunked transcription ---

@pytest.mark.asyncio
async def test_transcribe_short_audio_single_segment():
    """Audio < 25s should be sent as a single segment."""
    # 10 seconds of PCM at 16kHz mono 16-bit = 320000 bytes
    pcm_10s = b"\x00\x01" * 160000

    with patch("src.hub.voice_services.ogg_to_pcm", new_callable=AsyncMock) as mock_ogg, \
         patch("src.hub.voice_services.httpx.AsyncClient") as mock_client_cls:
        mock_ogg.return_value = pcm_10s

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"text": "phrase courte", "words": []}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await transcribe(b"fake-ogg", "http://stt:8432/v1/stt")

    assert result == "phrase courte"
    assert mock_client.post.call_count == 1


@pytest.mark.asyncio
async def test_transcribe_long_audio_multiple_segments():
    """Audio > 25s should be split into multiple segments."""
    # 50 seconds of PCM = 1600000 bytes -> 2 segments (25s + 25s)
    pcm_50s = b"\x00\x01" * 800000

    with patch("src.hub.voice_services.ogg_to_pcm", new_callable=AsyncMock) as mock_ogg, \
         patch("src.hub.voice_services.httpx.AsyncClient") as mock_client_cls:
        mock_ogg.return_value = pcm_50s

        mock_resp1 = MagicMock()
        mock_resp1.json.return_value = {"text": "premiere partie", "words": []}
        mock_resp1.raise_for_status = MagicMock()

        mock_resp2 = MagicMock()
        mock_resp2.json.return_value = {"text": "deuxieme partie", "words": []}
        mock_resp2.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.side_effect = [mock_resp1, mock_resp2]
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await transcribe(b"fake-ogg", "http://stt:8432/v1/stt")

    assert result == "premiere partie deuxieme partie"
    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_transcribe_initial_prompt_chaining():
    """Segment N's transcription should be passed as initial_prompt to segment N+1."""
    # 60s of PCM -> 3 segments
    pcm_60s = b"\x00\x01" * 960000

    with patch("src.hub.voice_services.ogg_to_pcm", new_callable=AsyncMock) as mock_ogg, \
         patch("src.hub.voice_services.httpx.AsyncClient") as mock_client_cls:
        mock_ogg.return_value = pcm_60s

        responses = []
        for text in ["un", "deux", "trois"]:
            r = MagicMock()
            r.json.return_value = {"text": text, "words": []}
            r.raise_for_status = MagicMock()
            responses.append(r)

        mock_client = AsyncMock()
        mock_client.post.side_effect = responses
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await transcribe(b"ogg", "http://stt:8432/v1/stt")

    assert result == "un deux trois"
    # Check that 2nd call has initial_prompt from 1st result
    second_call_payload = mock_client.post.call_args_list[1][1]["json"]
    assert second_call_payload.get("initial_prompt") == "un"


@pytest.mark.asyncio
async def test_transcribe_hallucination_filtered():
    """Segments detected as hallucinations should be excluded."""
    pcm_50s = b"\x00\x01" * 800000

    with patch("src.hub.voice_services.ogg_to_pcm", new_callable=AsyncMock) as mock_ogg, \
         patch("src.hub.voice_services.httpx.AsyncClient") as mock_client_cls:
        mock_ogg.return_value = pcm_50s

        mock_resp1 = MagicMock()
        mock_resp1.json.return_value = {"text": "vrai texte", "words": []}
        mock_resp1.raise_for_status = MagicMock()

        mock_resp2 = MagicMock()
        mock_resp2.json.return_value = {"text": "Sous-titrage ST", "words": []}
        mock_resp2.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.side_effect = [mock_resp1, mock_resp2]
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await transcribe(b"ogg", "http://stt:8432/v1/stt")

    assert result == "vrai texte"
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hub/test_voice_services.py::test_transcribe_long_audio_multiple_segments -v`
Expected: FAIL (current `transcribe()` sends one POST regardless of length)

**Step 3: Rewrite `transcribe()` in `src/hub/voice_services.py`**

Replace the existing `transcribe` function (lines 78-104) with:

```python
# Constants
CHUNK_DURATION_S = 25  # Whisper's native window is 30s, 25s for safety margin
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # 16-bit PCM
CHUNK_BYTES = CHUNK_DURATION_S * SAMPLE_RATE * BYTES_PER_SAMPLE  # 800000


async def transcribe(ogg_bytes: bytes, stt_url: str, language: str = "fr") -> str:
    """Transcribe OGG voice message to text via Whisper STT endpoint.

    For audio > 25s, splits into segments aligned with Whisper's 30s window
    and chains initial_prompt for contextual continuity.
    """
    from src.hub.hallucination_filter import is_hallucination

    pcm_data = await ogg_to_pcm(ogg_bytes)
    if not pcm_data:
        raise RuntimeError("ffmpeg produced empty PCM output")

    duration_s = len(pcm_data) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
    segments = _split_pcm(pcm_data)
    logger.info(
        "STT: %.1fs audio, %d segment(s), %.1f KB PCM",
        duration_s, len(segments), len(pcm_data) / 1024,
    )

    transcriptions: list[str] = []
    prev_text = ""

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
        for i, segment in enumerate(segments):
            seg_duration = len(segment) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
            audio_b64 = base64.b64encode(segment).decode()

            payload: dict = {
                "audio_base64": audio_b64,
                "sample_rate": SAMPLE_RATE,
                "language": language,
                "word_timestamps": True,
            }
            if prev_text:
                payload["initial_prompt"] = prev_text[-200:]

            logger.info(
                "STT segment %d/%d: %.1fs, %.1f KB base64",
                i + 1, len(segments), seg_duration, len(audio_b64) / 1024,
            )

            resp = await client.post(stt_url, json=payload)
            resp.raise_for_status()
            data = resp.json()

            text = data.get("text", "").strip()
            if not text:
                continue

            reason = is_hallucination(text)
            if reason:
                logger.info("STT segment %d/%d REJECTED: %s", i + 1, len(segments), reason)
                continue

            transcriptions.append(text)
            prev_text = text

    result = " ".join(transcriptions)
    if not result:
        raise RuntimeError("STT returned empty transcription")
    return result


def _split_pcm(pcm_data: bytes) -> list[bytes]:
    """Split PCM data into segments of CHUNK_DURATION_S seconds."""
    if len(pcm_data) <= CHUNK_BYTES:
        return [pcm_data]

    segments = []
    offset = 0
    while offset < len(pcm_data):
        end = min(offset + CHUNK_BYTES, len(pcm_data))
        segments.append(pcm_data[offset:end])
        offset = end
    return segments
```

**Step 4: Run all voice_services tests**

Run: `pytest tests/test_hub/test_voice_services.py -v`
Expected: all PASS (existing tests use mocked ogg_to_pcm returning short data, new tests verify chunking)

**Step 5: Commit**

```bash
git add src/hub/voice_services.py tests/test_hub/test_voice_services.py
git commit -m "feat(stt): chunked transcription with initial_prompt chaining and hallucination filter"
```

---

## Task 3: TTS Sentence Chunking

**Files:**
- Modify: `src/hub/voice_services.py` (replace `synthesize()`)
- Modify: `tests/test_hub/test_voice_services.py` (add TTS chunking tests)

**Step 1: Write the failing tests**

Append to `tests/test_hub/test_voice_services.py`:

```python
from src.hub.voice_services import _split_sentences


def test_split_sentences_basic():
    text = "Premiere phrase. Deuxieme phrase! Troisieme phrase?"
    result = _split_sentences(text)
    assert result == ["Premiere phrase.", "Deuxieme phrase!", "Troisieme phrase?"]


def test_split_sentences_long_sentence():
    """Sentences > 250 chars should be split on commas or spaces."""
    long = "A" * 300
    result = _split_sentences(long)
    assert all(len(s) <= 250 for s in result)
    assert "".join(result) == long


def test_split_sentences_short_text():
    result = _split_sentences("Bonjour")
    assert result == ["Bonjour"]


def test_split_sentences_empty():
    assert _split_sentences("") == []
    assert _split_sentences("   ") == []


@pytest.mark.asyncio
async def test_synthesize_long_text_chunked():
    """Long text should be split into sentences and each synthesized separately."""
    vc = VoiceConfig(enabled=True, tts_url="http://tts:8433/v1/tts", tts_language="fr")
    long_text = "Premiere phrase assez longue. Deuxieme phrase tout aussi longue. Troisieme."

    with patch("src.hub.voice_services.httpx.AsyncClient") as mock_client_cls, \
         patch("src.hub.voice_services.pcm_to_ogg", new_callable=AsyncMock) as mock_pcm:

        mock_resp = MagicMock()
        mock_resp.content = b"\x00" * 100
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        mock_pcm.return_value = b"OggS-output"

        result = await synthesize(long_text, vc)

    assert result == b"OggS-output"
    # Should have called TTS 3 times (one per sentence)
    assert mock_client.post.call_count == 3
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_hub/test_voice_services.py::test_split_sentences_basic -v`
Expected: FAIL with "cannot import name '_split_sentences'"

**Step 3: Rewrite `synthesize()` and add `_split_sentences()`**

Replace `synthesize` function (lines 107-133) in `src/hub/voice_services.py`:

```python
MAX_TTS_CHUNK = 250  # Max chars per TTS request (aligned with mnemos)

_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')


def _split_sentences(text: str) -> list[str]:
    """Split text into sentence chunks, each <= MAX_TTS_CHUNK chars."""
    text = text.strip()
    if not text:
        return []

    sentences = _SENTENCE_RE.split(text)
    chunks: list[str] = []

    for sentence in sentences:
        if len(sentence) <= MAX_TTS_CHUNK:
            chunks.append(sentence)
        else:
            # Split long sentences on commas, then by word boundary
            while sentence:
                if len(sentence) <= MAX_TTS_CHUNK:
                    chunks.append(sentence)
                    break
                # Try comma split
                cut = sentence.rfind(", ", 0, MAX_TTS_CHUNK)
                if cut == -1:
                    cut = sentence.rfind(" ", 0, MAX_TTS_CHUNK)
                if cut == -1:
                    cut = MAX_TTS_CHUNK
                else:
                    cut += 1  # include the space
                chunks.append(sentence[:cut].rstrip())
                sentence = sentence[cut:].lstrip()

    return chunks


async def synthesize(text: str, voice_config: VoiceConfig) -> bytes:
    """Synthesize text to OGG Opus audio via TTS endpoint.

    Splits long text into sentences (max 250 chars each) to avoid
    TTS server failures on long inputs.
    """
    chunks = _split_sentences(text)
    if not chunks:
        raise RuntimeError("No text to synthesize")

    all_pcm = bytearray()

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
        for chunk in chunks:
            payload: dict[str, Any] = {
                "text": chunk,
                "language": voice_config.tts_language,
                "sample_rate": 16000,
                "speed": voice_config.tts_speed,
            }
            if voice_config.tts_instruct:
                payload["instruct"] = voice_config.tts_instruct

            resp = await client.post(voice_config.tts_url, json=payload)
            resp.raise_for_status()

            if resp.content:
                all_pcm.extend(resp.content)

    if not all_pcm:
        raise RuntimeError("TTS returned empty audio")

    return await pcm_to_ogg(bytes(all_pcm), sample_rate=16000)
```

Add `import re` at the top of `voice_services.py` if not already present.

**Step 4: Run all voice_services tests**

Run: `pytest tests/test_hub/test_voice_services.py -v`
Expected: all PASS

Note: the existing `test_synthesize_success` test sends short text ("Bonjour") which becomes 1 chunk — should still pass. The existing `test_synthesize_empty_audio` test might need adjustment since `synthesize` now opens a context manager differently.

**Step 5: Commit**

```bash
git add src/hub/voice_services.py tests/test_hub/test_voice_services.py
git commit -m "feat(tts): sentence-based chunking for long text synthesis (max 250 chars)"
```

---

## Task 4: Telegram Markdown Sanitization + Message Splitting

**Files:**
- Modify: `src/hub/telegram_bot.py` (add `_sanitize_markdown_v1`, `_split_message`)
- Create: `tests/test_hub/test_telegram_helpers.py`

**Step 1: Write the failing tests**

```python
# tests/test_hub/test_telegram_helpers.py
"""Tests for Telegram message formatting helpers."""

from src.hub.telegram_bot import _sanitize_markdown_v1, _split_message


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
    # Underscores in identifiers should not break Telegram
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
    # All parts should fit
    assert all(len(p) <= 25 for p in result)
    # Content preserved
    assert "Para one." in result[0]


def test_split_message_no_good_boundary():
    text = "A" * 100
    result = _split_message(text, max_len=40)
    assert len(result) == 3
    assert all(len(p) <= 40 for p in result)
    assert "".join(result) == text


def test_split_message_empty():
    assert _split_message("", max_len=4000) == []
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hub/test_telegram_helpers.py -v`
Expected: FAIL with "cannot import name '_sanitize_markdown_v1'"

**Step 3: Implement in `src/hub/telegram_bot.py`**

Add these functions after the existing `_tg_esc` function (around line 30):

```python
import re as _re


def _sanitize_markdown_v1(text: str) -> str:
    """Sanitize text for Telegram MarkdownV1 compatibility.

    - Converts **bold** (V2) to *bold* (V1)
    - Escapes unmatched _ characters
    """
    # Convert **bold** to *bold*
    text = _re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)

    # Escape unmatched underscores (not inside _italic_ pairs)
    # Count underscores — if odd, escape the last one
    parts = text.split('_')
    if len(parts) % 2 == 0:
        # Odd number of underscores — escape standalone ones
        # Simple heuristic: if _ is surrounded by word chars, escape it
        text = _re.sub(r'(?<=\w)_(?=\w)', r'\_', text)

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
```

**Step 4: Run tests**

Run: `pytest tests/test_hub/test_telegram_helpers.py -v`
Expected: all PASS

**Step 5: Commit**

```bash
git add src/hub/telegram_bot.py tests/test_hub/test_telegram_helpers.py
git commit -m "feat(telegram): markdown V1 sanitization and message splitting"
```

---

## Task 5: Refactor on_dispatch Response Delivery

**Files:**
- Modify: `src/hub/main.py` (lines 389-416 in `on_dispatch`)

**Step 1: Read the current code**

The current code at lines 389-416 does:
1. Builds `full_output` with header
2. Truncates at 4000 chars
3. Calls `thinking_msg.edit_text(full_output, parse_mode="Markdown")`
4. Falls back to plain text, then `reply_text`

**Step 2: Refactor response delivery**

Replace lines 389-416 in `src/hub/main.py` with:

```python
        # Build final message with status header
        if status == "completed":
            header = f"\u2705 *Termine* ({total_time})"
        elif status == "failed":
            header = f"\u274c *Echec* ({total_time})"
        elif status == "timeout":
            header = ""
        else:
            header = f"\U0001f4e8 *Reponse* ({total_time})"

        # Update thinking message with final status (short)
        try:
            status_emoji = {
                "completed": "\u2705", "failed": "\u274c",
                "timeout": "\u23f0",
            }.get(status, "\U0001f4e8")
            await thinking_msg.edit_text(
                f"{status_emoji} *{status.title()}* ({total_time})",
                parse_mode="Markdown",
            )
        except Exception:
            pass

        # Send response as NEW message(s) for visibility
        from src.hub.telegram_bot import _sanitize_markdown_v1, _split_message

        if header:
            full_output = f"{header}\n\n{output}"
        else:
            full_output = output

        parts = _split_message(full_output)
        for part in parts:
            sanitized = _sanitize_markdown_v1(part)
            try:
                await update.message.reply_text(sanitized, parse_mode="Markdown")
            except Exception:
                try:
                    await update.message.reply_text(part)
                except Exception as e:
                    logger.warning("Failed to send response part: %s", e)
```

**Step 3: Run existing tests**

Run: `pytest tests/ -v --ignore=tests/test_daemon/test_mcp_server.py --ignore=tests/test_hub/test_telegram_bot.py -x`
Expected: all PASS (on_dispatch is tested via integration, not unit)

**Step 4: Commit**

```bash
git add src/hub/main.py
git commit -m "fix(telegram): send response as new message instead of editing progress message"
```

---

## Task 6: ActiveConversationManager

**Files:**
- Create: `src/hub/active_conversations.py`
- Create: `tests/test_hub/test_active_conversations.py`

**Step 1: Write the failing tests**

```python
# tests/test_hub/test_active_conversations.py
"""Tests for active conversation tracking."""

import time

from src.hub.active_conversations import ActiveConversation, ActiveConversationManager


def test_start_conversation():
    mgr = ActiveConversationManager()
    mgr.start(user_id=123, mission_id="m1", daemon_url="http://d:7701")
    active = mgr.get_active(123)
    assert active is not None
    assert active.mission_id == "m1"
    assert active.status == "active"


def test_get_active_none():
    mgr = ActiveConversationManager()
    assert mgr.get_active(999) is None


def test_touch_updates_activity():
    mgr = ActiveConversationManager()
    mgr.start(123, "m1", "http://d:7701")
    t1 = mgr.get_active(123).last_activity
    time.sleep(0.01)
    mgr.touch(123)
    t2 = mgr.get_active(123).last_activity
    assert t2 > t1


def test_close_conversation():
    mgr = ActiveConversationManager()
    mgr.start(123, "m1", "http://d:7701")
    mgr.close(123)
    assert mgr.get_active(123) is None


def test_start_replaces_existing():
    mgr = ActiveConversationManager()
    mgr.start(123, "m1", "http://d:7701")
    mgr.start(123, "m2", "http://d:7701")
    assert mgr.get_active(123).mission_id == "m2"


def test_cleanup_stale():
    mgr = ActiveConversationManager()
    mgr.start(123, "m1", "http://d:7701")
    # Force stale
    mgr._active[123].last_activity = time.time() - 700
    mgr.cleanup_stale(ttl=600)
    assert mgr.get_active(123) is None


def test_cleanup_keeps_fresh():
    mgr = ActiveConversationManager()
    mgr.start(123, "m1", "http://d:7701")
    mgr.cleanup_stale(ttl=600)
    assert mgr.get_active(123) is not None


def test_is_injectable_recent():
    mgr = ActiveConversationManager()
    mgr.start(123, "m1", "http://d:7701")
    assert mgr.is_injectable(123) is True


def test_is_injectable_stale():
    mgr = ActiveConversationManager()
    mgr.start(123, "m1", "http://d:7701")
    mgr._active[123].started_at = time.time() - 700
    mgr._active[123].last_activity = time.time() - 700
    assert mgr.is_injectable(123) is False
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hub/test_active_conversations.py -v`
Expected: FAIL with "ModuleNotFoundError"

**Step 3: Implement**

```python
# src/hub/active_conversations.py
"""Track active dispatcher conversations per Telegram user."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

CONVERSATION_TTL = 600  # 10 minutes


@dataclass
class ActiveConversation:
    user_id: int
    mission_id: str
    daemon_url: str
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    status: Literal["active", "completed", "failed"] = "active"


class ActiveConversationManager:
    """Tracks one active conversation per user."""

    def __init__(self) -> None:
        self._active: dict[int, ActiveConversation] = {}

    def start(self, user_id: int, mission_id: str, daemon_url: str) -> None:
        self._active[user_id] = ActiveConversation(
            user_id=user_id,
            mission_id=mission_id,
            daemon_url=daemon_url,
        )

    def get_active(self, user_id: int) -> ActiveConversation | None:
        conv = self._active.get(user_id)
        if conv and conv.status == "active":
            return conv
        return None

    def touch(self, user_id: int) -> None:
        conv = self._active.get(user_id)
        if conv:
            conv.last_activity = time.time()

    def close(self, user_id: int) -> None:
        self._active.pop(user_id, None)

    def cleanup_stale(self, ttl: int = CONVERSATION_TTL) -> None:
        now = time.time()
        stale = [
            uid for uid, conv in self._active.items()
            if now - conv.last_activity > ttl
        ]
        for uid in stale:
            logger.info("Closing stale conversation for user %d (mission %s)", uid, self._active[uid].mission_id)
            del self._active[uid]

    def is_injectable(self, user_id: int) -> bool:
        """Check if a message can be injected into the active conversation."""
        conv = self.get_active(user_id)
        if not conv:
            return False
        return time.time() - conv.last_activity < CONVERSATION_TTL
```

**Step 4: Run tests**

Run: `pytest tests/test_hub/test_active_conversations.py -v`
Expected: all 9 PASS

**Step 5: Commit**

```bash
git add src/hub/active_conversations.py tests/test_hub/test_active_conversations.py
git commit -m "feat(dispatcher): add ActiveConversationManager for supervised conversations"
```

---

## Task 7: Wire Conversation Manager into on_dispatch

**Files:**
- Modify: `src/hub/main.py` (top of `on_dispatch`, add conversation injection logic)

**Step 1: Add imports and initialization**

In `src/hub/main.py`, add import near the top (after existing imports):

```python
from src.hub.active_conversations import ActiveConversationManager
```

In the `start_hub` function, before the `on_dispatch` definition (~line 180), add:

```python
    conversation_manager = ActiveConversationManager()
```

**Step 2: Add conversation injection at the start of `on_dispatch`**

After the `if not config.dispatcher.get("enabled"):` check, add:

```python
        user_id = update.effective_user.id

        # Check for active conversation to inject into
        if conversation_manager.is_injectable(user_id):
            active = conversation_manager.get_active(user_id)
            try:
                async with httpx.AsyncClient(timeout=10) as inject_client:
                    resp = await inject_client.post(
                        f"{active.daemon_url}/api/session/deliver",
                        json={
                            "mission_id": active.mission_id,
                            "message": text,
                            "from": "human",
                        },
                    )
                    if resp.status_code == 200:
                        conversation_manager.touch(user_id)
                        if conv_store:
                            conv_store.add_message(user_id=user_id, role="user", content=text)
                        await update.message.reply_text(
                            f"\U0001f4ac _Injecte dans la conversation active_",
                            parse_mode="Markdown",
                        )
                        return
            except Exception as e:
                logger.warning("Failed to inject into active conversation: %s", e)
                conversation_manager.close(user_id)
                # Fall through to new mission
```

**Step 3: Track conversation on new mission launch**

After the `mission_id = str(uuid.uuid4())` line (~line 222), add:

```python
        conversation_manager.start(user_id, mission_id, machine["daemon_url"])
```

After the polling loop completes and response is sent (after the `for part in parts:` block), add:

```python
        # Close conversation if mission completed or failed
        if status in ("completed", "failed"):
            conversation_manager.close(user_id)
```

**Step 4: Run existing tests**

Run: `pytest tests/ -v --ignore=tests/test_daemon/test_mcp_server.py --ignore=tests/test_hub/test_telegram_bot.py -x`
Expected: all PASS

**Step 5: Commit**

```bash
git add src/hub/main.py
git commit -m "feat(dispatcher): wire conversation manager into on_dispatch with injection support"
```

---

## Task 8: Dispatcher Preferences Backend

**Files:**
- Modify: `src/hub/attention_store.py` (add dispatcher prefs, same pattern as TTS prefs)
- Modify: `src/hub/attention_api.py` (add GET/PATCH endpoints)
- Create: `tests/test_hub/test_dispatcher_prefs.py`

**Step 1: Write the failing tests**

```python
# tests/test_hub/test_dispatcher_prefs.py
"""Tests for dispatcher preferences in AttentionStore."""

import json
import tempfile
from pathlib import Path

from src.hub.attention_store import AttentionStore


def test_dispatcher_prefs_defaults():
    store = AttentionStore(prefs_path=tempfile.mktemp())
    prefs = store.get_dispatcher_prefs()
    assert prefs["conversation_active"] is True
    assert prefs["show_agent_exchanges"] is True
    assert prefs["voice_response"] is True
    assert prefs["auto_print_pos"] is False
    assert prefs["hear_agents"] is False


def test_dispatcher_prefs_update():
    store = AttentionStore(prefs_path=tempfile.mktemp())
    updated = store.update_dispatcher_prefs({"hear_agents": True})
    assert updated["hear_agents"] is True
    assert updated["conversation_active"] is True  # unchanged


def test_dispatcher_prefs_persist():
    with tempfile.TemporaryDirectory() as td:
        prefs_path = str(Path(td) / "notification_prefs.json")
        store = AttentionStore(prefs_path=prefs_path)
        store.update_dispatcher_prefs({"auto_print_pos": True})

        # Reload
        store2 = AttentionStore(prefs_path=prefs_path)
        prefs = store2.get_dispatcher_prefs()
        assert prefs["auto_print_pos"] is True


def test_dispatcher_prefs_ignores_unknown_keys():
    store = AttentionStore(prefs_path=tempfile.mktemp())
    updated = store.update_dispatcher_prefs({"unknown_key": True})
    assert "unknown_key" not in updated
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hub/test_dispatcher_prefs.py -v`
Expected: FAIL with "AttributeError: 'AttentionStore' object has no attribute 'get_dispatcher_prefs'"

**Step 3: Add dispatcher prefs to AttentionStore**

In `src/hub/attention_store.py`, add after `_DEFAULT_TTS_PREFS` (~line 47):

```python
    _DEFAULT_DISPATCHER_PREFS: dict[str, bool] = {
        "conversation_active": True,
        "show_agent_exchanges": True,
        "voice_response": True,
        "auto_print_pos": False,
        "hear_agents": False,
    }
```

In `__init__`, add after `self._tts_prefs` init:

```python
        self._dispatcher_prefs: dict[str, bool] = dict(self._DEFAULT_DISPATCHER_PREFS)
        self._load_dispatcher_prefs()
```

Add the load/save/get/update methods (same pattern as TTS prefs):

```python
    # ------------------------------------------------------------------
    # Dispatcher preferences
    # ------------------------------------------------------------------

    def _load_dispatcher_prefs(self) -> None:
        path = Path(self._prefs_path).parent / "dispatcher_prefs.json"
        if path.is_file():
            try:
                with open(path) as f:
                    data = json.load(f)
                for key in self._DEFAULT_DISPATCHER_PREFS:
                    if key in data:
                        self._dispatcher_prefs[key] = bool(data[key])
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load dispatcher prefs: %s", e)

    def _save_dispatcher_prefs(self) -> None:
        path = Path(self._prefs_path).parent / "dispatcher_prefs.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(self._dispatcher_prefs, f, indent=2)
        except OSError as e:
            logger.warning("Failed to save dispatcher prefs: %s", e)

    def get_dispatcher_prefs(self) -> dict[str, bool]:
        return dict(self._dispatcher_prefs)

    def update_dispatcher_prefs(self, updates: dict) -> dict[str, bool]:
        for key in self._DEFAULT_DISPATCHER_PREFS:
            if key in updates:
                self._dispatcher_prefs[key] = bool(updates[key])
        self._save_dispatcher_prefs()
        return self.get_dispatcher_prefs()
```

**Step 4: Add API endpoints**

In `src/hub/attention_api.py`, add after the `update_tts_prefs` endpoint (~line 435):

```python
    @router.get("/dispatcher-prefs")
    async def get_dispatcher_prefs():
        """Return current dispatcher preferences."""
        return store.get_dispatcher_prefs()

    @router.patch("/dispatcher-prefs")
    async def update_dispatcher_prefs(request: Request):
        """Update dispatcher preferences (partial merge)."""
        updates = await request.json()
        updated = store.update_dispatcher_prefs(updates)
        await store.broadcast({"type": "dispatcher_prefs_updated", "dispatcher_prefs": updated})
        return updated
```

**Step 5: Run tests**

Run: `pytest tests/test_hub/test_dispatcher_prefs.py -v`
Expected: all 4 PASS

Also: `pytest tests/test_hub/ -v -x`
Expected: all PASS (no regressions)

**Step 6: Commit**

```bash
git add src/hub/attention_store.py src/hub/attention_api.py tests/test_hub/test_dispatcher_prefs.py
git commit -m "feat(prefs): add dispatcher preferences backend with persistence"
```

---

## Task 9: PWA Dispatcher Preferences UI

**Files:**
- Modify: `pwa/index.html` (add Dispatcher section in prefs panel)
- Modify: `pwa/app.js` (add dispatcher pref sync)

**Step 1: Add HTML for dispatcher prefs section**

In `pwa/index.html`, after the TTS prefs section (look for the closing `</div>` of TTS toggles, before `</aside>`), add:

```html
    <div class="prefs-separator">Dispatcher</div>
    <div class="prefs-section">
      <div class="prefs-row">
        <span class="prefs-row-text">Conversation active</span>
        <label class="toggle">
          <input type="checkbox" id="pref-conversation-active" checked>
          <span class="toggle-track"></span>
        </label>
      </div>
      <div class="prefs-row">
        <span class="prefs-row-text">Echanges agents visibles</span>
        <label class="toggle">
          <input type="checkbox" id="pref-show-agent-exchanges" checked>
          <span class="toggle-track"></span>
        </label>
      </div>
      <div class="prefs-row">
        <span class="prefs-row-text">Reponse vocale</span>
        <label class="toggle">
          <input type="checkbox" id="pref-voice-response" checked>
          <span class="toggle-track"></span>
        </label>
      </div>
      <div class="prefs-row">
        <span class="prefs-row-text">Auto-print POS</span>
        <label class="toggle">
          <input type="checkbox" id="pref-auto-print-pos">
          <span class="toggle-track"></span>
        </label>
      </div>
      <div class="prefs-row">
        <span class="prefs-row-text">Entendre les agents</span>
        <label class="toggle">
          <input type="checkbox" id="pref-hear-agents">
          <span class="toggle-track"></span>
        </label>
      </div>
    </div>
```

**Step 2: Add JS sync in `pwa/app.js`**

Find the `PREF_DEFAULTS` object and add:

```javascript
    'pref-conversation-active': true,
    'pref-show-agent-exchanges': true,
    'pref-voice-response': true,
    'pref-auto-print-pos': false,
    'pref-hear-agents': false,
```

Find where TTS prefs are synced to the server (look for `tts-prefs` PATCH call). Add a similar block for dispatcher prefs:

```javascript
  // Map pref IDs to dispatcher-prefs API keys
  var DISPATCHER_PREF_MAP = {
    'pref-conversation-active': 'conversation_active',
    'pref-show-agent-exchanges': 'show_agent_exchanges',
    'pref-voice-response': 'voice_response',
    'pref-auto-print-pos': 'auto_print_pos',
    'pref-hear-agents': 'hear_agents',
  };

  function isDispatcherPref(key) {
    return key in DISPATCHER_PREF_MAP;
  }
```

In the pref change handler, after the TTS sync block, add:

```javascript
    if (isDispatcherPref(key)) {
      var body = {};
      body[DISPATCHER_PREF_MAP[key]] = value;
      fetch('/api/attention/dispatcher-prefs', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    }
```

In the WebSocket message handler, add handling for `dispatcher_prefs_updated`:

```javascript
    if (msg.type === 'dispatcher_prefs_updated' && msg.dispatcher_prefs) {
      var dp = msg.dispatcher_prefs;
      for (var prefId in DISPATCHER_PREF_MAP) {
        var apiKey = DISPATCHER_PREF_MAP[prefId];
        if (apiKey in dp) {
          savePref(prefId, dp[apiKey]);
        }
      }
      syncPrefsToDOM();
    }
```

**Step 3: Test manually**

1. Rebuild hub: `docker compose -f docker-compose.hub.yml build --no-cache && docker compose -f docker-compose.hub.yml up -d`
2. Open PWA at `https://attention.robotsinlove.be`
3. Verify "Dispatcher" section appears in prefs panel
4. Toggle "Entendre les agents" ON
5. Verify via: `curl -s http://localhost:7700/api/attention/dispatcher-prefs | python3 -m json.tool`

**Step 4: Commit**

```bash
git add pwa/index.html pwa/app.js
git commit -m "feat(pwa): add dispatcher preferences section with server sync"
```

---

## Task 10: Agent Voice Styles

**Files:**
- Modify: `config/config.yml` (add voice_styles section)
- Modify: `src/hub/main.py` (synthesize inter-agent messages with per-agent voices)

**Step 1: Add voice_styles to config**

In `config/config.yml`, add after the `voice:` section:

```yaml
voice_styles:
  default: "Speak with a warm, natural native French female voice with clear pronunciation and moderate pace."
  dispatcher: "Speak with a warm, natural native French female voice with clear pronunciation and moderate pace."
  agent_project: "Parle avec une voix masculine calme et posee, en francais."
  agent_infra: "Parle avec une voix masculine technique et precise, en francais."
```

**Step 2: Add voice style resolution to on_dispatch**

In `src/hub/main.py`, in the inter-agent message forwarding section (to be wired in Task 7), add a helper:

```python
def _get_voice_style(agent_id: str, config: IntercomConfig) -> str:
    """Resolve voice style for an agent based on config."""
    styles = config.raw.get("voice_styles", {})
    # Check for agent-specific style first
    if agent_id in styles:
        return styles[agent_id]
    # Categorize by convention
    if "infra" in agent_id or "admin" in agent_id:
        return styles.get("agent_infra", styles.get("default", ""))
    return styles.get("agent_project", styles.get("default", ""))
```

**Step 3: Wire TTS synthesis for inter-agent messages**

In the section where inter-agent messages are forwarded to Telegram (part of the router callback), add TTS synthesis when `hear_agents` pref is ON:

```python
# In the message forwarding callback:
dispatcher_prefs = attention_store.get_dispatcher_prefs()
if dispatcher_prefs.get("hear_agents") and voice_config.enabled:
    try:
        style = _get_voice_style(from_agent, config)
        tts_vc = VoiceConfig(
            enabled=True,
            tts_url=voice_config.tts_url,
            tts_language=voice_config.tts_language,
            tts_speed=voice_config.tts_speed,
            tts_instruct=style,
        )
        ogg = await synthesize(message_text[:500], tts_vc)
        await attention_store.broadcast({
            "type": "tts_audio",
            "audio_b64": base64.b64encode(ogg).decode(),
            "agent": from_agent,
        })
    except Exception:
        logger.debug("Agent voice TTS failed for %s", from_agent)
```

**Step 4: Test manually**

1. Rebuild hub
2. Open PWA, enable "Entendre les agents" in prefs
3. Send a Telegram message that triggers inter-agent routing
4. Verify TTS audio is broadcast to PWA

**Step 5: Commit**

```bash
git add config/config.yml src/hub/main.py
git commit -m "feat(voice): per-agent voice styles with TTS synthesis for inter-agent messages"
```

---

## Task 11: Integration Test and Final Cleanup

**Files:**
- Run full test suite
- Update `CLAUDE.md` with new features
- Rebuild and deploy hub

**Step 1: Run full test suite**

Run: `pytest tests/ -v --ignore=tests/test_daemon/test_mcp_server.py --ignore=tests/test_hub/test_telegram_bot.py`
Expected: all PASS (395+ existing + ~30 new tests)

**Step 2: Manual end-to-end test**

1. Rebuild hub: `docker compose -f docker-compose.hub.yml build --no-cache && docker compose -f docker-compose.hub.yml up -d`
2. Send a voice message on Telegram (~30s+)
3. Verify: full transcription (no truncation), response as new message, readable Markdown
4. Send a follow-up text message within 10 min
5. Verify: injected into active conversation
6. Check PWA prefs panel has "Dispatcher" section
7. Check hub logs for STT segment logging

**Step 3: Commit design doc and plan**

```bash
git add docs/plans/
git commit -m "docs: add design and implementation plan for voice conversation pipeline"
```

**Step 4: Final deployment**

```bash
docker compose -f docker-compose.hub.yml build --no-cache
docker compose -f docker-compose.hub.yml up -d
# Verify logs
docker logs ai-intercom-hub --tail 20
```
