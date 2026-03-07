"""Voice services for AI-Intercom: STT and TTS via Jetson Thor endpoints."""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# STT chunking constants — Whisper's window is 30s, 25s for safety margin
CHUNK_DURATION_S = 25
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # 16-bit PCM
CHUNK_BYTES = CHUNK_DURATION_S * SAMPLE_RATE * BYTES_PER_SAMPLE  # 800000


@dataclass
class VoiceConfig:
    """Configuration for voice services (STT/TTS)."""

    enabled: bool = False
    stt_url: str = ""
    tts_url: str = ""
    tts_language: str = "fr"
    tts_speed: float = 1.0
    tts_instruct: str = ""
    response_voice: bool = True


def parse_voice_config(raw: dict[str, Any] | None) -> VoiceConfig:
    """Parse voice configuration from YAML dict."""
    if not raw:
        return VoiceConfig()
    return VoiceConfig(
        enabled=bool(raw.get("enabled", False)),
        stt_url=raw.get("stt_url", ""),
        tts_url=raw.get("tts_url", ""),
        tts_language=raw.get("tts_language", "fr"),
        tts_speed=float(raw.get("tts_speed", 1.0)),
        tts_instruct=raw.get("tts_instruct", ""),
        response_voice=bool(raw.get("response_voice", True)),
    )


async def _run_ffmpeg(args: list[str], input_data: bytes) -> bytes:
    """Run ffmpeg with stdin/stdout piping."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(input_data), timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (code {proc.returncode}): {stderr.decode()[-200:]}")
    return stdout


async def ogg_to_pcm(ogg_bytes: bytes) -> bytes:
    """Convert OGG Opus audio to raw PCM 16kHz mono s16le."""
    return await _run_ffmpeg(
        ["-i", "pipe:0", "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1"],
        ogg_bytes,
    )


async def pcm_to_ogg(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
    """Convert raw PCM s16le audio to OGG Opus."""
    return await _run_ffmpeg(
        [
            "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
            "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", "64k", "-f", "ogg", "pipe:1",
        ],
        pcm_bytes,
    )


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


async def synthesize(text: str, voice_config: VoiceConfig) -> bytes:
    """Synthesize text to OGG Opus audio via CosyVoice TTS endpoint.

    Pipeline: text -> POST /v1/tts -> raw PCM 16kHz -> ffmpeg -> OGG Opus

    CosyVoice handles long text natively and resamples server-side,
    so we request 16kHz directly. The `instruct` parameter controls voice style.
    """
    payload: dict[str, Any] = {
        "text": text,
        "language": voice_config.tts_language,
        "sample_rate": 16000,
        "speed": voice_config.tts_speed,
    }
    if voice_config.tts_instruct:
        payload["instruct"] = voice_config.tts_instruct

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(voice_config.tts_url, json=payload)
        resp.raise_for_status()

    pcm_data = resp.content
    if not pcm_data:
        raise RuntimeError("TTS returned empty audio")

    return await pcm_to_ogg(pcm_data, sample_rate=16000)
