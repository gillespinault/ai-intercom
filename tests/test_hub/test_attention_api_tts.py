"""Tests for the POST /api/attention/tts proxy endpoint."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from fastapi import FastAPI

from src.hub.attention_store import AttentionStore
from src.hub.attention_api import create_attention_router


@pytest.fixture
def app():
    store = AttentionStore()
    registry = AsyncMock()
    app = FastAPI()
    router = create_attention_router(store, registry)
    app.include_router(router)
    app.state.tts_url = "http://jetson-thor:8431"
    app.state.attention_store = store
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_tts_proxy_returns_audio(client):
    """Mock httpx call and verify PCM audio bytes are returned."""
    fake_pcm = b"\x00\x01" * 1024

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = fake_pcm

    mock_client_instance = AsyncMock()
    mock_client_instance.post = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    with patch("src.hub.attention_api.httpx.AsyncClient", return_value=mock_client_instance):
        resp = client.post("/api/attention/tts", json={"text": "Bonjour le monde", "language": "fr"})

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/raw"
    assert resp.content == fake_pcm

    # Verify the XTTS service was called with correct payload
    mock_client_instance.post.assert_called_once_with(
        "http://jetson-thor:8431/v1/tts",
        json={"text": "Bonjour le monde", "language": "fr", "sample_rate": 24000},
    )


def test_tts_proxy_rejects_empty_text(client):
    """Empty or whitespace-only text should return 400."""
    resp = client.post("/api/attention/tts", json={"text": "", "language": "fr"})
    assert resp.status_code == 400
    assert "text is required" in resp.json()["error"]

    resp2 = client.post("/api/attention/tts", json={"text": "   ", "language": "fr"})
    assert resp2.status_code == 400


def test_tts_proxy_rate_limited(client):
    """Second request within 2 seconds should be rejected with 429."""
    fake_pcm = b"\x00\x01" * 512

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = fake_pcm

    mock_client_instance = AsyncMock()
    mock_client_instance.post = AsyncMock(return_value=mock_response)
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=False)

    with patch("src.hub.attention_api.httpx.AsyncClient", return_value=mock_client_instance):
        # First request: should succeed
        resp1 = client.post("/api/attention/tts", json={"text": "Premier", "language": "fr"})
        assert resp1.status_code == 200

        # Second request immediately: should be rate limited
        resp2 = client.post("/api/attention/tts", json={"text": "Deuxieme", "language": "fr"})
        assert resp2.status_code == 429
        assert "Rate limited" in resp2.json()["error"]


def test_tts_proxy_no_url_configured():
    """When tts_url is empty, should return 503."""
    store = AttentionStore()
    registry = AsyncMock()
    app = FastAPI()
    router = create_attention_router(store, registry)
    app.include_router(router)
    app.state.tts_url = ""
    app.state.attention_store = store

    no_url_client = TestClient(app)
    resp = no_url_client.post("/api/attention/tts", json={"text": "Salut", "language": "fr"})
    assert resp.status_code == 503
    assert "not configured" in resp.json()["error"]
