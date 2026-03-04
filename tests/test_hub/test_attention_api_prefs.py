"""Tests for notification prefs REST API endpoints."""

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI
from unittest.mock import MagicMock

from src.hub.attention_api import create_attention_router
from src.hub.attention_store import AttentionStore
from src.hub.registry import Registry


@pytest.fixture
def app(tmp_path):
    store = AttentionStore(prefs_path=str(tmp_path / "prefs.json"))
    registry = MagicMock(spec=Registry)
    router = create_attention_router(store, registry)
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestPrefsAPI:
    def test_get_prefs_returns_defaults(self, client):
        resp = client.get("/api/attention/prefs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["permission"] is True
        assert data["question"] is True
        assert data["text_input"] is True

    def test_patch_prefs_updates(self, client):
        resp = client.patch("/api/attention/prefs", json={"question": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["question"] is False
        assert data["permission"] is True

    def test_patch_prefs_persists(self, client):
        client.patch("/api/attention/prefs", json={"text_input": False})
        resp = client.get("/api/attention/prefs")
        assert resp.json()["text_input"] is False

    def test_patch_prefs_ignores_unknown(self, client):
        resp = client.patch("/api/attention/prefs", json={"foo": True})
        assert resp.status_code == 200
        assert "foo" not in resp.json()
