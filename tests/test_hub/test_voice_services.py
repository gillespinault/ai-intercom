"""Tests for voice services module (STT/TTS)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.hub.voice_services import (
    VoiceConfig,
    ogg_to_pcm,
    parse_voice_config,
    pcm_to_ogg,
    synthesize,
    transcribe,
)


# --- parse_voice_config ---


def test_parse_voice_config_empty():
    cfg = parse_voice_config({})
    assert cfg.enabled is False
    assert cfg.stt_url == ""
    assert cfg.tts_url == ""
    assert cfg.tts_language == "fr"


def test_parse_voice_config_none():
    cfg = parse_voice_config(None)
    assert cfg.enabled is False


def test_parse_voice_config_full():
    cfg = parse_voice_config({
        "enabled": True,
        "stt_url": "http://host:8432/v1/stt",
        "tts_url": "http://host:8431/v1/tts",
        "tts_language": "en",
        "tts_speed": 1.5,
        "tts_instruct": "speak calmly",
        "response_voice": False,
    })
    assert cfg.enabled is True
    assert cfg.stt_url == "http://host:8432/v1/stt"
    assert cfg.tts_url == "http://host:8431/v1/tts"
    assert cfg.tts_language == "en"
    assert cfg.tts_speed == 1.5
    assert cfg.tts_instruct == "speak calmly"
    assert cfg.response_voice is False


def test_parse_voice_config_defaults():
    cfg = parse_voice_config({"enabled": True, "stt_url": "http://x"})
    assert cfg.tts_speed == 1.0
    assert cfg.tts_language == "fr"
    assert cfg.response_voice is True


# --- ogg_to_pcm ---


@pytest.mark.asyncio
async def test_ogg_to_pcm_success():
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"\x00\x01" * 100, b"")
    mock_proc.returncode = 0

    with patch("src.hub.voice_services.asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await ogg_to_pcm(b"fake-ogg-data")
    assert result == b"\x00\x01" * 100


@pytest.mark.asyncio
async def test_ogg_to_pcm_ffmpeg_failure():
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"", b"Error: invalid data")
    mock_proc.returncode = 1

    with patch("src.hub.voice_services.asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(RuntimeError, match="ffmpeg failed"):
            await ogg_to_pcm(b"bad-data")


@pytest.mark.asyncio
async def test_ogg_to_pcm_timeout():
    import asyncio

    mock_proc = AsyncMock()
    mock_proc.communicate.side_effect = asyncio.TimeoutError()

    with patch("src.hub.voice_services.asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(asyncio.TimeoutError):
            await ogg_to_pcm(b"data")


# --- pcm_to_ogg ---


@pytest.mark.asyncio
async def test_pcm_to_ogg_success():
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"OggS-fake", b"")
    mock_proc.returncode = 0

    with patch("src.hub.voice_services.asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await pcm_to_ogg(b"\x00" * 1600, sample_rate=16000)
    assert result == b"OggS-fake"


@pytest.mark.asyncio
async def test_pcm_to_ogg_failure():
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (b"", b"codec error")
    mock_proc.returncode = 1

    with patch("src.hub.voice_services.asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(RuntimeError, match="ffmpeg failed"):
            await pcm_to_ogg(b"\x00" * 100)


# --- transcribe ---


@pytest.mark.asyncio
async def test_transcribe_success():
    with patch("src.hub.voice_services.ogg_to_pcm", new_callable=AsyncMock) as mock_ogg, \
         patch("src.hub.voice_services.httpx.AsyncClient") as mock_client_cls:
        mock_ogg.return_value = b"\x00\x01" * 50

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"text": "Bonjour le monde"}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await transcribe(b"fake-ogg", "http://stt:8432/v1/stt")

    assert result == "Bonjour le monde"
    mock_ogg.assert_called_once_with(b"fake-ogg")


@pytest.mark.asyncio
async def test_transcribe_empty_pcm():
    with patch("src.hub.voice_services.ogg_to_pcm", new_callable=AsyncMock) as mock_ogg:
        mock_ogg.return_value = b""

        with pytest.raises(RuntimeError, match="empty PCM"):
            await transcribe(b"ogg", "http://stt:8432/v1/stt")


@pytest.mark.asyncio
async def test_transcribe_api_error():
    with patch("src.hub.voice_services.ogg_to_pcm", new_callable=AsyncMock) as mock_ogg, \
         patch("src.hub.voice_services.httpx.AsyncClient") as mock_client_cls:
        mock_ogg.return_value = b"\x00" * 50

        import httpx
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            await transcribe(b"ogg", "http://stt:8432/v1/stt")


# --- synthesize ---


@pytest.mark.asyncio
async def test_synthesize_success():
    pcm_data = b"\x00\x01" * 100

    vc = VoiceConfig(
        enabled=True,
        tts_url="http://tts:8431/v1/tts",
        tts_language="fr",
        tts_speed=1.0,
    )

    with patch("src.hub.voice_services.httpx.AsyncClient") as mock_client_cls, \
         patch("src.hub.voice_services.pcm_to_ogg", new_callable=AsyncMock) as mock_pcm:
        mock_resp = MagicMock()
        mock_resp.content = pcm_data
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        mock_pcm.return_value = b"OggS-output"

        result = await synthesize("Bonjour", vc)

    assert result == b"OggS-output"
    mock_pcm.assert_called_once_with(pcm_data, sample_rate=16000)


@pytest.mark.asyncio
async def test_synthesize_empty_audio():
    vc = VoiceConfig(enabled=True, tts_url="http://tts:8431/v1/tts")

    with patch("src.hub.voice_services.httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.content = b""
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        with pytest.raises(RuntimeError, match="empty audio"):
            await synthesize("Hello", vc)
