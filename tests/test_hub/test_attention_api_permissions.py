"""Tests for hub permission API endpoints."""

import pytest
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI

from src.hub.attention_store import AttentionStore
from src.hub.attention_api import create_attention_router
from src.hub.registry import Registry
from src.shared.models import PermissionRequest


@pytest.fixture
def store():
    return AttentionStore()


@pytest.fixture
async def registry(tmp_path):
    db_path = str(tmp_path / "test_registry.db")
    reg = Registry(db_path=db_path)
    await reg.init()
    yield reg
    await reg.close()


@pytest.fixture
def app(store, registry):
    app = FastAPI()
    app.include_router(create_attention_router(store, registry))
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestPermissionEndpoints:
    @pytest.mark.anyio
    async def test_post_permission_request(self, client, store):
        resp = await client.post("/api/attention/permission", json={
            "machine": "laptop",
            "session_id": "sess-1",
            "tool_name": "Bash",
            "tool_input": {"command": "docker ps"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "request_id" in data
        assert store.get_pending_permission(data["request_id"]) is not None

    @pytest.mark.anyio
    async def test_decide_allow(self, client, store):
        req = PermissionRequest(
            session_id="sess-1", tool_name="Bash",
            tool_input={"command": "ls"}, machine="laptop",
        )
        store.add_pending_permission(req)

        resp = await client.post(
            f"/api/attention/permission/{req.request_id}/decide",
            json={"decision": "allow"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resolved"
        assert store.get_pending_permission(req.request_id) is None

    @pytest.mark.anyio
    async def test_decide_deny(self, client, store):
        req = PermissionRequest(
            session_id="sess-1", tool_name="Bash",
            tool_input={"command": "rm -rf /"}, machine="laptop",
        )
        store.add_pending_permission(req)

        resp = await client.post(
            f"/api/attention/permission/{req.request_id}/decide",
            json={"decision": "deny", "reason": "Dangerous"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resolved"

    @pytest.mark.anyio
    async def test_decide_not_found(self, client):
        resp = await client.post(
            "/api/attention/permission/nonexistent/decide",
            json={"decision": "allow"},
        )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_list_pending(self, client, store):
        req = PermissionRequest(
            session_id="sess-1", tool_name="Bash",
            tool_input={}, machine="laptop",
        )
        store.add_pending_permission(req)

        resp = await client.get("/api/attention/permission/pending")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["pending"]) == 1
        assert data["pending"][0]["tool_name"] == "Bash"
