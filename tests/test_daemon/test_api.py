import pytest
from httpx import ASGITransport, AsyncClient
from src.daemon.api import create_app
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
