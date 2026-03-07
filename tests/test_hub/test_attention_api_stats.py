"""Tests for usage stats API endpoint and WebSocket broadcast."""
from __future__ import annotations

import json
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.hub.attention_api import create_attention_router
from src.hub.attention_store import AttentionStore
from src.hub.registry import Registry


@pytest.fixture
def app():
    store = AttentionStore(prefs_path="/tmp/test_stats_prefs.json")
    registry = Registry.__new__(Registry)
    registry._machines = {}
    registry._lock = asyncio.Lock()
    app = FastAPI()
    app.include_router(create_attention_router(store, registry))
    app.state.store = store
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_post_stats(client):
    stats = {
        "machine_id": "serverlab",
        "stats": {
            "block": {
                "start_time": "2026-03-04T14:00:00Z",
                "end_time": "2026-03-04T19:00:00Z",
                "elapsed_pct": 50.0,
                "remaining_minutes": 150,
                "reset_time": "19:00",
                "is_active": True,
            },
            "weekly": {"total_tokens": 1000000000, "display": "1.0B"},
            "sessions": {
                "sess-1": {"context_percent": 45.0, "context_tokens": 90000}
            },
        },
    }
    resp = client.post("/api/attention/stats", json=stats)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_get_stats(client):
    stats_payload = {
        "machine_id": "serverlab",
        "stats": {
            "block": {
                "start_time": "2026-03-04T14:00:00Z",
                "end_time": "2026-03-04T19:00:00Z",
                "elapsed_pct": 50.0,
                "remaining_minutes": 150,
                "reset_time": "19:00",
                "is_active": True,
            },
            "weekly": {"total_tokens": 500000000, "display": "500M"},
            "sessions": {},
        },
    }
    client.post("/api/attention/stats", json=stats_payload)
    resp = client.get("/api/attention/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["block"]["elapsed_pct"] == 50.0
    assert data["weekly"]["display"] == "500M"


def test_snapshot_includes_stats(client):
    stats_payload = {
        "machine_id": "serverlab",
        "stats": {
            "block": {"elapsed_pct": 75.0, "is_active": True,
                      "start_time": "", "end_time": "",
                      "remaining_minutes": 75, "reset_time": "19:00"},
            "weekly": {"total_tokens": 100, "display": "100"},
            "sessions": {},
        },
    }
    client.post("/api/attention/stats", json=stats_payload)
    with client.websocket_connect("/api/attention/ws") as ws:
        snapshot = json.loads(ws.receive_text())
        assert snapshot["type"] == "snapshot"
        assert "usage_stats" in snapshot
        assert snapshot["usage_stats"]["block"]["elapsed_pct"] == 75.0
