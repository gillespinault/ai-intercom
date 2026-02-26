import pytest
from httpx import ASGITransport, AsyncClient
from src.hub.hub_api import create_hub_api
from src.hub.registry import Registry
from src.shared.auth import sign_request
from unittest.mock import AsyncMock


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
