"""Tests for the GET /api/attention/presence endpoint."""

import pytest
from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from fastapi import FastAPI

from src.hub.attention_store import AttentionStore
from src.hub.attention_api import create_attention_router


@pytest.fixture
def store():
    return AttentionStore()


@pytest.fixture
def app(store):
    registry = AsyncMock()
    app = FastAPI()
    router = create_attention_router(store, registry)
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_presence_no_clients(client):
    """Returns 0 clients and default TTS prefs when no connections."""
    resp = client.get("/api/attention/presence")
    assert resp.status_code == 200
    data = resp.json()
    assert data["connected_clients"] == 0
    assert data["active_sessions"] == 0
    assert data["tts"]["enabled"] is True
    assert data["tts"]["categories"]["milestone"] is True


def test_presence_with_sessions(client, store):
    """Returns session count when sessions are tracked."""
    from src.shared.models import AttentionSession, AttentionState

    store.handle_event("test-machine", {
        "type": "new_session",
        "session": AttentionSession(
            session_id="s1",
            pid=1234,
            machine="test-machine",
            project="test-project",
            state=AttentionState.WORKING,
        ),
    })
    resp = client.get("/api/attention/presence")
    data = resp.json()
    assert data["connected_clients"] == 0
    assert data["active_sessions"] == 1
    assert "tts" in data


def test_tts_prefs_patch_and_presence(client, store):
    """TTS prefs update via PATCH is reflected in presence."""
    resp = client.patch(
        "/api/attention/tts-prefs",
        json={"enabled": False, "categories": {"didactic": False}},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert resp.json()["categories"]["didactic"] is False
    # milestone untouched
    assert resp.json()["categories"]["milestone"] is True

    # Verify presence reflects the change
    resp2 = client.get("/api/attention/presence")
    assert resp2.json()["tts"]["enabled"] is False
    assert resp2.json()["tts"]["categories"]["didactic"] is False
