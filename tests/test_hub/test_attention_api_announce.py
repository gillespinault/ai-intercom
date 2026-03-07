"""Tests for the /api/attention/announce broadcast endpoint."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.hub.attention_api import create_attention_router
from src.hub.attention_store import AttentionStore
from src.hub.registry import Registry


@pytest.fixture
def app():
    store = AttentionStore()
    registry = Registry.__new__(Registry)
    registry._machines = {}
    registry._lock = asyncio.Lock()
    app = FastAPI()
    app.include_router(create_attention_router(store, registry))
    app.state.tts_url = ""
    app.state.attention_store = store
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_announce_broadcasts(client):
    """POST /api/attention/announce with a valid message returns 200 + ok."""
    resp = client.post("/api/attention/announce", json={
        "machine_id": "serverlab",
        "session_id": "sess-123",
        "project": "AI-intercom",
        "message": "Build succeeded",
        "category": "milestone",
        "priority": "normal",
    })
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_announce_rejects_empty_message(client):
    """POST /api/attention/announce with empty message returns 400."""
    resp = client.post("/api/attention/announce", json={
        "machine_id": "serverlab",
        "message": "",
    })
    assert resp.status_code == 400
    assert resp.json()["error"] == "message is required"


def test_announce_rejects_whitespace_only_message(client):
    """POST /api/attention/announce with whitespace-only message returns 400."""
    resp = client.post("/api/attention/announce", json={
        "message": "   ",
    })
    assert resp.status_code == 400


def test_announce_rejects_missing_message(client):
    """POST /api/attention/announce with no message field returns 400."""
    resp = client.post("/api/attention/announce", json={
        "machine_id": "serverlab",
        "session_id": "sess-1",
    })
    assert resp.status_code == 400


def test_announce_defaults(client):
    """POST /api/attention/announce with only message uses defaults for optional fields."""
    resp = client.post("/api/attention/announce", json={
        "message": "Tests passed",
    })
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
