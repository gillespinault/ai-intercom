"""Tests for dispatcher conversation history API endpoint."""

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

from src.hub.conversation_store import ConversationStore


@pytest.fixture
def store(tmp_path):
    s = ConversationStore(db_path=str(tmp_path / "conv.db"))
    s.init()
    s.add_message(user_id=123, role="user", content="deploy backup")
    s.add_message(user_id=123, role="assistant", content="Backup deployed successfully")
    s.add_message(user_id=123, role="user", content="check disk space")
    return s


@pytest.fixture
def app(store):
    """Create a minimal FastAPI app with the dispatcher history endpoint."""
    from src.hub.hub_api import create_hub_api
    from src.hub.registry import Registry

    # We create a standalone app that mirrors the real endpoint logic
    app = FastAPI()

    @app.get("/api/dispatcher/history")
    def get_history(user_id: int, query: str = "", limit: int = 5):
        if query:
            return {"messages": store.search(user_id, query, limit)}
        return {"messages": store.get_history(user_id, limit)}

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestHistoryEndpoint:
    def test_get_history(self, client):
        resp = client.get("/api/dispatcher/history", params={"user_id": 123, "limit": 10})
        assert resp.status_code == 200
        assert len(resp.json()["messages"]) == 3

    def test_search_history(self, client):
        resp = client.get("/api/dispatcher/history", params={"user_id": 123, "query": "backup"})
        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert len(msgs) == 2
        assert all("backup" in m["content"].lower() for m in msgs)

    def test_search_no_results(self, client):
        resp = client.get("/api/dispatcher/history", params={"user_id": 123, "query": "nonexistent"})
        assert resp.status_code == 200
        assert resp.json()["messages"] == []

    def test_different_user_id(self, client):
        resp = client.get("/api/dispatcher/history", params={"user_id": 999, "limit": 10})
        assert resp.status_code == 200
        assert resp.json()["messages"] == []

    def test_limit_respected(self, client):
        resp = client.get("/api/dispatcher/history", params={"user_id": 123, "limit": 2})
        assert resp.status_code == 200
        # get_history returns the last N messages, so we should get the 2 most recent
        assert len(resp.json()["messages"]) == 2
