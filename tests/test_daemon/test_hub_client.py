import pytest
from src.daemon.hub_client import HubClient


@pytest.fixture
def client():
    return HubClient(
        hub_url="http://localhost:7700",
        token="test-token",
        machine_id="test-machine",
    )


def test_client_initialization(client):
    assert client.hub_url == "http://localhost:7700"
    assert client.machine_id == "test-machine"


def test_client_builds_auth_headers(client):
    headers = client._auth_headers(b"test body")
    assert "X-Intercom-Machine" in headers
    assert "X-Intercom-Signature" in headers


@pytest.mark.asyncio
async def test_route_chat(httpx_mock):
    httpx_mock.add_response(
        url="http://hub:7700/api/route",
        json={"status": "delivered", "thread_id": "t-new1", "mission_id": "m-chat-001"},
    )
    client = HubClient("http://hub:7700", "token", "serverlab")
    result = await client.route_chat(
        from_agent="serverlab/ai-intercom",
        to="limn/mnemos",
        message="hello",
    )
    assert result["status"] == "delivered"
    assert result["thread_id"] == "t-new1"


@pytest.mark.asyncio
async def test_route_chat_with_thread_id(httpx_mock):
    httpx_mock.add_response(
        url="http://hub:7700/api/route",
        json={"status": "delivered", "thread_id": "t-existing"},
    )
    client = HubClient("http://hub:7700", "token", "serverlab")
    result = await client.route_chat(
        from_agent="serverlab/ai-intercom",
        to="limn/mnemos",
        message="follow-up",
        thread_id="t-existing",
    )
    assert result["thread_id"] == "t-existing"


@pytest.mark.asyncio
async def test_route_reply(httpx_mock):
    httpx_mock.add_response(
        url="http://hub:7700/api/route",
        json={"status": "delivered", "thread_id": "t-existing"},
    )
    client = HubClient("http://hub:7700", "token", "serverlab")
    result = await client.route_reply(
        from_agent="serverlab/ai-intercom",
        thread_id="t-existing",
        message="reply here",
    )
    assert result["status"] == "delivered"


@pytest.mark.asyncio
async def test_push_feedback(httpx_mock):
    httpx_mock.add_response(
        url="http://hub:7700/api/missions/m-001/feedback",
        json={"status": "ok"},
    )
    client = HubClient("http://hub:7700", "token", "serverlab")
    result = await client.push_feedback(
        mission_id="m-001",
        feedback=[{"timestamp": "2026-03-01T10:00:00Z", "kind": "tool", "summary": "Reading file"}],
        turn_count=2,
        status="running",
    )
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_push_result(httpx_mock):
    httpx_mock.add_response(
        url="http://hub:7700/api/missions/m-002/result",
        json={"status": "ok"},
    )
    client = HubClient("http://hub:7700", "token", "serverlab")
    result = await client.push_result(
        mission_id="m-002",
        status="completed",
        output="Agent finished successfully",
        feedback=[],
        started_at="2026-03-01T10:00:00Z",
        finished_at="2026-03-01T10:05:00Z",
        turn_count=5,
    )
    assert result["status"] == "ok"
