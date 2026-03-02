import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from src.hub.hub_api import create_hub_api
from src.hub.registry import Registry
from src.shared.auth import sign_request
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
async def registry(tmp_path):
    reg = Registry(str(tmp_path / "test.db"))
    await reg.init()
    yield reg
    await reg.close()


@pytest.fixture
def app(registry):
    from src.shared.config import IntercomConfig
    config = IntercomConfig(mode="hub", auth={"hub_token": "hub-secret"})
    return create_hub_api(registry, router=AsyncMock(), config=config)


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_discover_endpoint(client):
    resp = await client.get("/api/discover")
    assert resp.status_code == 200
    assert resp.json()["hub"] is True


async def test_join_creates_pending(client, registry):
    resp = await client.post("/api/join", json={
        "machine_id": "new-machine",
        "display_name": "New Machine",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending_approval"


async def test_heartbeat(client, registry):
    await registry.register_machine("vps", "VPS", "1.2.3.4", "http://1.2.3.4:7700", "tok")
    body = b'{"machine_id": "vps"}'
    headers = sign_request(body, "vps", "tok")
    resp = await client.post("/api/heartbeat", content=body, headers=headers)
    assert resp.status_code == 200


async def test_heartbeat_with_version(client, registry):
    await registry.register_machine("vps", "VPS", "1.2.3.4", "http://1.2.3.4:7700", "tok")
    body = json.dumps({"machine_id": "vps", "version": "0.4.0"}).encode()
    headers = sign_request(body, "vps", "tok")
    resp = await client.post("/api/heartbeat", content=body, headers=headers)
    assert resp.status_code == 200
    machine = await registry.get_machine("vps")
    assert machine["version"] == "0.4.0"


# --- Chat routing tests ---


def _chat_route_body(
    from_agent: str = "vps/AI-intercom",
    to_agent: str = "laptop/my-project",
    message: str = "Hello from vps",
    thread_id: str = "t-001",
) -> bytes:
    """Build a JSON body for a chat route request."""
    return json.dumps({
        "from_agent": from_agent,
        "to_agent": to_agent,
        "type": "chat",
        "payload": {"message": message, "thread_id": thread_id},
    }).encode()


async def _register_machines(registry: Registry) -> None:
    """Register source (vps) and target (laptop) machines."""
    await registry.register_machine(
        "vps", "VPS Server", "10.0.0.1", "http://10.0.0.1:7700", "tok-vps"
    )
    await registry.register_machine(
        "laptop", "Laptop", "10.0.0.2", "http://10.0.0.2:7700", "tok-laptop"
    )


def _mock_httpx_response(status_code: int, json_data: dict):
    """Create a mock httpx response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    return mock_resp


async def test_route_chat_delivered(client, registry):
    """Chat message delivered to daemon with active session (daemon responds 200)."""
    await _register_machines(registry)

    body = _chat_route_body()
    headers = sign_request(body, "vps", "tok-vps")

    mock_resp = _mock_httpx_response(200, {"status": "delivered"})
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.hub.hub_api.httpx.AsyncClient", return_value=mock_client):
        resp = await client.post("/api/route", content=body, headers=headers)

    data = resp.json()
    assert resp.status_code == 200
    assert data["status"] == "delivered"
    assert data["thread_id"] == "t-001"
    assert "mission_id" in data

    # Verify the daemon was called with correct URL and payload
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert call_args[0][0] == "http://10.0.0.2:7700/api/session/deliver"
    payload = call_args[1]["json"]
    assert payload["project"] == "my-project"
    assert payload["thread_id"] == "t-001"
    assert payload["from_agent"] == "vps/AI-intercom"
    assert payload["message"] == "Hello from vps"


async def test_route_chat_no_session(client, registry):
    """Daemon returns 404 (no active session), hub returns no_active_session."""
    await _register_machines(registry)

    body = _chat_route_body()
    headers = sign_request(body, "vps", "tok-vps")

    mock_resp = _mock_httpx_response(404, {"error": "no session"})
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.hub.hub_api.httpx.AsyncClient", return_value=mock_client):
        resp = await client.post("/api/route", content=body, headers=headers)

    data = resp.json()
    assert resp.status_code == 200
    assert data["status"] == "no_active_session"
    assert data["thread_id"] == "t-001"


async def test_route_chat_reply_resolves_recipient(client, registry):
    """Reply with empty to_agent resolves recipient from thread_store."""
    await _register_machines(registry)

    # First: send a chat message to populate thread_store
    body1 = _chat_route_body(
        from_agent="vps/AI-intercom", to_agent="laptop/my-project",
        thread_id="t-reply-test", message="initial",
    )
    headers1 = sign_request(body1, "vps", "tok-vps")

    mock_resp = _mock_httpx_response(200, {"status": "delivered"})
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_resp
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.hub.hub_api.httpx.AsyncClient", return_value=mock_client):
        await client.post("/api/route", content=body1, headers=headers1)

    # Now: reply with empty to_agent (simulating intercom_reply)
    reply_body = json.dumps({
        "from_agent": "laptop/my-project",
        "to_agent": "",
        "type": "chat",
        "payload": {"message": "reply msg", "thread_id": "t-reply-test"},
    }).encode()
    headers2 = sign_request(reply_body, "laptop", "tok-laptop")

    mock_client2 = AsyncMock()
    mock_client2.post.return_value = mock_resp
    mock_client2.__aenter__ = AsyncMock(return_value=mock_client2)
    mock_client2.__aexit__ = AsyncMock(return_value=False)

    with patch("src.hub.hub_api.httpx.AsyncClient", return_value=mock_client2):
        resp = await client.post("/api/route", content=reply_body, headers=headers2)

    data = resp.json()
    assert resp.status_code == 200
    assert data["status"] == "delivered"

    # Verify the reply was routed to vps (the other participant)
    call_args = mock_client2.post.call_args
    assert call_args[0][0] == "http://10.0.0.1:7700/api/session/deliver"
    payload = call_args[1]["json"]
    assert payload["from_agent"] == "laptop/my-project"
    assert payload["message"] == "reply msg"


async def test_route_chat_reply_unknown_thread(client, registry):
    """Reply with empty to_agent and unknown thread_id returns error."""
    await _register_machines(registry)

    body = json.dumps({
        "from_agent": "vps/AI-intercom",
        "to_agent": "",
        "type": "chat",
        "payload": {"message": "orphan reply", "thread_id": "t-unknown"},
    }).encode()
    headers = sign_request(body, "vps", "tok-vps")

    resp = await client.post("/api/route", content=body, headers=headers)
    data = resp.json()
    assert resp.status_code == 200
    assert data["status"] == "error"
    assert "resolve" in data["error"].lower() or "thread" in data["error"].lower()


async def test_route_chat_unknown_machine(client, registry):
    """Target machine not in registry returns error."""
    # Only register the source machine, NOT the target
    await registry.register_machine(
        "vps", "VPS Server", "10.0.0.1", "http://10.0.0.1:7700", "tok-vps"
    )

    body = _chat_route_body()
    headers = sign_request(body, "vps", "tok-vps")

    resp = await client.post("/api/route", content=body, headers=headers)

    data = resp.json()
    assert resp.status_code == 200
    assert data["status"] == "error"
    assert "laptop" in data["error"]


# --- Push model: receive endpoints ---


async def test_receive_feedback(client, registry):
    """POST /api/missions/{id}/feedback stores feedback in mission_store."""
    await _register_machines(registry)

    app_state = client._transport.app.state
    app_state.mission_store["m-fb-1"] = [{
        "from_agent": "vps/proj", "to_agent": "laptop/proj",
        "type": "ask", "mission_id": "m-fb-1",
    }]

    body = json.dumps({
        "machine_id": "laptop",
        "feedback": [
            {"timestamp": "2026-03-01T10:00:30Z", "kind": "tool", "summary": "Reading config.py"},
        ],
        "turn_count": 2,
        "status": "running",
    }).encode()
    headers = sign_request(body, "laptop", "tok-laptop")

    resp = await client.post("/api/missions/m-fb-1/feedback", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    history = app_state.mission_store["m-fb-1"]
    feedback_entry = [m for m in history if m.get("type") == "feedback"]
    assert len(feedback_entry) == 1
    assert feedback_entry[0]["payload"]["turn_count"] == 2


async def test_receive_result(client, registry):
    """POST /api/missions/{id}/result stores final result in mission_store."""
    await _register_machines(registry)

    app_state = client._transport.app.state
    app_state.mission_store["m-res-1"] = [{
        "from_agent": "vps/proj", "to_agent": "laptop/proj",
        "type": "ask", "mission_id": "m-res-1",
    }]

    body = json.dumps({
        "machine_id": "laptop",
        "status": "completed",
        "output": "Done! Here is the result.",
        "feedback": [],
        "started_at": "2026-03-01T10:00:00Z",
        "finished_at": "2026-03-01T10:05:00Z",
        "turn_count": 5,
    }).encode()
    headers = sign_request(body, "laptop", "tok-laptop")

    resp = await client.post("/api/missions/m-res-1/result", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    history = app_state.mission_store["m-res-1"]
    result_entry = [m for m in history if m.get("type") == "result"]
    assert len(result_entry) == 1
    assert result_entry[0]["payload"]["status"] == "completed"
    assert result_entry[0]["payload"]["output"] == "Done! Here is the result."


async def test_receive_result_unknown_mission(client, registry):
    """POST /api/missions/{id}/result for unknown mission returns 404."""
    await _register_machines(registry)

    body = json.dumps({
        "machine_id": "laptop",
        "status": "completed",
        "output": "orphan result",
        "feedback": [],
        "started_at": "2026-03-01T10:00:00Z",
        "finished_at": "2026-03-01T10:05:00Z",
        "turn_count": 1,
    }).encode()
    headers = sign_request(body, "laptop", "tok-laptop")

    resp = await client.post("/api/missions/m-unknown/result", content=body, headers=headers)
    assert resp.status_code == 404


async def test_mission_status_from_store(client, registry):
    """GET /api/missions/{id}/status returns data from mission_store."""
    app_state = client._transport.app.state
    app_state.mission_store["m-st-1"] = [
        {"from_agent": "vps/proj", "to_agent": "laptop/proj", "type": "ask", "mission_id": "m-st-1"},
        {"type": "result", "payload": {
            "status": "completed",
            "output": "All done",
            "feedback": [{"timestamp": "...", "kind": "tool", "summary": "test"}],
            "started_at": "2026-03-01T10:00:00Z",
            "finished_at": "2026-03-01T10:05:00Z",
            "turn_count": 3,
        }},
    ]

    resp = await client.get("/api/missions/m-st-1/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["output"] == "All done"
    assert data["turn_count"] == 3


async def test_mission_status_launched(client, registry):
    """GET /api/missions/{id}/status for just-launched mission returns launched."""
    app_state = client._transport.app.state
    app_state.mission_store["m-new-1"] = [
        {"from_agent": "vps/proj", "to_agent": "laptop/proj", "type": "ask", "mission_id": "m-new-1"},
    ]

    resp = await client.get("/api/missions/m-new-1/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "launched"
    assert data["turn_count"] == 0
