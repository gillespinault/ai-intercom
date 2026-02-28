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


# --- Session endpoint tests ---

import json
import os
import tempfile

from httpx import ASGITransport, AsyncClient


async def _make_session_client():
    """Helper: create a fresh app + async client for session tests."""
    app = create_app("test-machine", "test-token")
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    return app, client


async def test_session_register():
    app, client = await _make_session_client()
    try:
        resp = await client.post("/api/session/register", json={
            "session_id": "sess-1",
            "project": "my-project",
            "pid": os.getpid(),
            "inbox_path": "/tmp/test-inbox-register.jsonl",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "registered"
        assert data["session_id"] == "sess-1"
        assert "sess-1" in app.state.active_sessions
    finally:
        await client.aclose()


async def test_session_register_then_list():
    app, client = await _make_session_client()
    try:
        await client.post("/api/session/register", json={
            "session_id": "sess-a",
            "project": "proj-a",
            "pid": os.getpid(),
            "inbox_path": "/tmp/test-inbox-list-a.jsonl",
        })
        await client.post("/api/session/register", json={
            "session_id": "sess-b",
            "project": "proj-b",
            "pid": os.getpid(),
            "inbox_path": "/tmp/test-inbox-list-b.jsonl",
        })
        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["sessions"]) == 2
        session_ids = [s["session_id"] for s in data["sessions"]]
        assert "sess-a" in session_ids
        assert "sess-b" in session_ids
    finally:
        await client.aclose()


async def test_session_unregister():
    app, client = await _make_session_client()
    inbox_file = None
    try:
        fd, inbox_file = tempfile.mkstemp(suffix=".jsonl", prefix="test-inbox-unreg-")
        os.close(fd)

        await client.post("/api/session/register", json={
            "session_id": "sess-del",
            "project": "proj-del",
            "pid": os.getpid(),
            "inbox_path": inbox_file,
        })
        assert "sess-del" in app.state.active_sessions

        resp = await client.post("/api/session/unregister", json={
            "session_id": "sess-del",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "unregistered"
        assert "sess-del" not in app.state.active_sessions
        # Inbox file should be cleaned up
        assert not os.path.exists(inbox_file)
    finally:
        await client.aclose()
        if inbox_file and os.path.exists(inbox_file):
            os.unlink(inbox_file)


async def test_session_deliver():
    app, client = await _make_session_client()
    inbox_file = None
    try:
        fd, inbox_file = tempfile.mkstemp(suffix=".jsonl", prefix="test-inbox-deliver-")
        os.close(fd)

        await client.post("/api/session/register", json={
            "session_id": "sess-dlv",
            "project": "proj-dlv",
            "pid": os.getpid(),
            "inbox_path": inbox_file,
        })

        resp = await client.post("/api/session/deliver", json={
            "project": "proj-dlv",
            "thread_id": "t-1",
            "from_agent": "server/other",
            "message": "Hello from other agent",
            "timestamp": "2026-02-28T12:00:00Z",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "delivered"

        # Verify JSONL content
        with open(inbox_file, "r") as f:
            lines = f.readlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["thread_id"] == "t-1"
        assert entry["from_agent"] == "server/other"
        assert entry["message"] == "Hello from other agent"
        assert entry["read"] is False
    finally:
        await client.aclose()
        if inbox_file and os.path.exists(inbox_file):
            os.unlink(inbox_file)


async def test_session_deliver_no_session():
    _, client = await _make_session_client()
    try:
        resp = await client.post("/api/session/deliver", json={
            "project": "nonexistent-project",
            "thread_id": "t-x",
            "from_agent": "server/other",
            "message": "Nobody home",
            "timestamp": "2026-02-28T12:00:00Z",
        })
        assert resp.status_code == 404
        data = resp.json()
        assert data["status"] == "no_active_session"
    finally:
        await client.aclose()


async def test_session_deliver_dead_pid():
    app, client = await _make_session_client()
    inbox_file = None
    try:
        fd, inbox_file = tempfile.mkstemp(suffix=".jsonl", prefix="test-inbox-dead-")
        os.close(fd)

        # Register with a PID that almost certainly doesn't exist
        await client.post("/api/session/register", json={
            "session_id": "sess-dead",
            "project": "proj-dead",
            "pid": 999999,
            "inbox_path": inbox_file,
        })

        resp = await client.post("/api/session/deliver", json={
            "project": "proj-dead",
            "thread_id": "t-dead",
            "from_agent": "server/other",
            "message": "Are you alive?",
            "timestamp": "2026-02-28T12:00:00Z",
        })
        assert resp.status_code == 404
        data = resp.json()
        assert data["status"] == "no_active_session"
        # Session should be cleaned up
        assert "sess-dead" not in app.state.active_sessions
    finally:
        await client.aclose()
        if inbox_file and os.path.exists(inbox_file):
            os.unlink(inbox_file)


async def test_session_status():
    app, client = await _make_session_client()
    inbox_file = None
    try:
        fd, inbox_file = tempfile.mkstemp(suffix=".jsonl", prefix="test-inbox-status-")
        os.close(fd)

        await client.post("/api/session/register", json={
            "session_id": "sess-st",
            "project": "proj-st",
            "pid": os.getpid(),
            "inbox_path": inbox_file,
        })

        # Deliver a message so inbox has content
        await client.post("/api/session/deliver", json={
            "session_id": "sess-st",
            "thread_id": "t-st",
            "from_agent": "server/other",
            "message": "status check msg",
            "timestamp": "2026-02-28T12:00:00Z",
        })

        resp = await client.get("/api/session/sess-st/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "sess-st"
        assert data["project"] == "proj-st"
        assert data["inbox_pending"] == 1
    finally:
        await client.aclose()
        if inbox_file and os.path.exists(inbox_file):
            os.unlink(inbox_file)
