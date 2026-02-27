import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from src.daemon.api import create_app
from src.daemon.agent_launcher import AgentLauncher
from src.shared.auth import sign_request


@pytest.fixture
def app():
    return create_app(machine_id="test-machine", token="test-token")


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["machine_id"] == "test-machine"
    assert data["status"] == "ok"


async def test_discover(client):
    resp = await client.get("/api/discover")
    assert resp.status_code == 200
    data = resp.json()
    assert data["hub"] is False  # daemon, not hub


async def test_message_requires_auth(client):
    resp = await client.post("/api/message", json={"test": True})
    assert resp.status_code == 401


async def test_message_with_valid_auth(client):
    body = b'{"type": "ask", "payload": {"message": "hello"}}'
    headers = sign_request(body, "hub", "test-token")
    resp = await client.post("/api/message", content=body, headers=headers)
    assert resp.status_code == 200


async def test_status_endpoint(client):
    resp = await client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "active_missions" in data


async def test_message_launches_background(app, client):
    """POST /api/message with launcher should return immediately."""
    launcher = AgentLauncher(
        default_command="echo",
        default_args=["done"],
        allowed_paths=["/tmp"],
        max_duration=10,
    )
    app.state.launcher = launcher
    app.state.project_paths = {"test-project": "/tmp"}

    body = b'{"type": "start_agent", "to_agent": "machine/test-project", "mission_id": "m-bg-1", "payload": {"mission": "hello"}}'
    headers = sign_request(body, "hub", "test-token")
    resp = await client.post("/api/message", content=body, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "launched"
    assert data["mission_id"] == "m-bg-1"
    # Output should NOT be in the immediate response (non-blocking)
    assert "output" not in data
    # Wait for background task to complete to avoid orphan
    await asyncio.sleep(1)


async def test_mission_status_endpoint(app, client):
    """GET /api/missions/{id} should return status from launcher."""
    launcher = AgentLauncher(
        default_command="echo",
        default_args=["test-output"],
        allowed_paths=["/tmp"],
        max_duration=10,
    )
    app.state.launcher = launcher
    app.state.project_paths = {"proj": "/tmp"}

    # Launch a background mission
    body = b'{"type": "start_agent", "to_agent": "m/proj", "mission_id": "m-status-1", "payload": {"mission": "hi"}}'
    headers = sign_request(body, "hub", "test-token")
    await client.post("/api/message", content=body, headers=headers)

    # Wait for completion
    await asyncio.sleep(2)

    # Check status
    resp = await client.get("/api/missions/m-status-1")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["output"] is not None


async def test_mission_status_not_found(app, client):
    launcher = AgentLauncher("echo", [], ["/tmp"], 10)
    app.state.launcher = launcher
    resp = await client.get("/api/missions/nonexistent")
    assert resp.status_code == 404
