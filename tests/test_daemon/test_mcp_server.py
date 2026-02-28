import json
import os
import tempfile

import pytest
from unittest.mock import AsyncMock
from src.daemon.mcp_server import IntercomTools


@pytest.fixture
def tools():
    return IntercomTools(
        hub_client=AsyncMock(),
        machine_id="serverlab",
        current_project="infra",
    )


async def test_list_agents(tools):
    tools.hub_client.list_agents.return_value = [
        {"machine": "vps", "project": "nginx", "status": "online"}
    ]
    result = await tools.list_agents(filter="online")
    assert len(result["agents"]) == 1
    tools.hub_client.list_agents.assert_called_once_with(filter="online")


async def test_send_message(tools):
    tools.hub_client.send_message.return_value = {"status": "sent"}
    result = await tools.send(to="vps/nginx", message="hello")
    assert result["status"] == "sent"


async def test_ask_returns_immediately_with_mission_id(tools):
    """ask() returns the route result immediately (no polling)."""
    tools.hub_client.ask.return_value = {
        "status": "launched",
        "mission_id": "m-launch-1",
    }
    result = await tools.ask(to="vps/nginx", message="do something", timeout=60)
    assert result["status"] == "launched"
    assert result["mission_id"] == "m-launch-1"
    # No polling should happen
    tools.hub_client.get_daemon_mission_status.assert_not_called()


async def test_ask_propagates_error(tools):
    """ask() should propagate error statuses from route."""
    tools.hub_client.ask.return_value = {
        "status": "denied",
        "mission_id": "m-denied",
    }
    result = await tools.ask(to="vps/nginx", message="do something")
    assert result["status"] == "denied"


async def test_register_update(tools):
    tools.hub_client.register.return_value = {"status": "updated"}
    result = await tools.register(
        action="update",
        project={"description": "Updated desc", "capabilities": ["new"]},
    )
    assert result["status"] == "updated"


async def test_report_feedback(tools):
    tools.hub_client.submit_feedback.return_value = {
        "status": "stored",
        "timestamp": "2026-02-27T12:00:00Z",
    }
    result = await tools.report_feedback(
        feedback_type="bug",
        description="intercom_ask times out",
        context="httpx.ReadTimeout after 30s",
    )
    assert result["status"] == "stored"
    tools.hub_client.submit_feedback.assert_called_once_with(
        from_agent="serverlab/infra",
        feedback_type="bug",
        description="intercom_ask times out",
        context="httpx.ReadTimeout after 30s",
    )


async def test_daemon_status(tools):
    tools.hub_client.get_daemon_mission_status.return_value = {
        "mission_id": "m-001",
        "status": "running",
    }
    result = await tools.daemon_status(mission_id="m-001")
    assert result["status"] == "running"


class FakeHubClientChat:
    """Fake hub client that handles chat operations."""

    def __init__(self):
        self.last_route = None

    async def list_agents(self, filter="all"):
        return []

    async def route_chat(self, from_agent, to, message):
        self.last_route = {"from": from_agent, "to": to, "message": message}
        return {"status": "delivered", "thread_id": "t-new123"}

    async def route_reply(self, from_agent, thread_id, message):
        self.last_route = {
            "from": from_agent,
            "thread_id": thread_id,
            "message": message,
        }
        return {"status": "delivered", "thread_id": thread_id}


@pytest.fixture
def chat_tools():
    client = FakeHubClientChat()
    tools = IntercomTools(client, "serverlab", "ai-intercom")
    return tools, client


@pytest.mark.asyncio
async def test_chat_sends_via_hub(chat_tools):
    tools, client = chat_tools
    result = await tools.chat(to="limn/mnemos", message="hello")
    assert result["status"] == "delivered"
    assert client.last_route["to"] == "limn/mnemos"


@pytest.mark.asyncio
async def test_reply_sends_via_hub(chat_tools):
    tools, client = chat_tools
    result = await tools.reply(thread_id="t-abc", message="world")
    assert result["status"] == "delivered"
    assert client.last_route["thread_id"] == "t-abc"


@pytest.mark.asyncio
async def test_check_inbox_empty(chat_tools):
    tools, _ = chat_tools
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        inbox_path = f.name
    try:
        tools._inbox_path = inbox_path
        result = await tools.check_inbox()
        assert result["count"] == 0
        assert result["messages"] == []
    finally:
        os.unlink(inbox_path)


@pytest.mark.asyncio
async def test_check_inbox_with_messages(chat_tools):
    tools, _ = chat_tools
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(
            json.dumps(
                {
                    "thread_id": "t-abc",
                    "from_agent": "limn/mnemos",
                    "timestamp": "2026-02-28T16:00:00Z",
                    "message": "hello",
                    "read": False,
                }
            )
            + "\n"
        )
        inbox_path = f.name
    try:
        tools._inbox_path = inbox_path
        result = await tools.check_inbox()
        assert result["count"] == 1
        assert result["messages"][0]["message"] == "hello"

        # Verify message marked as read
        with open(inbox_path) as f:
            data = json.loads(f.readline())
        assert data["read"] is True
    finally:
        os.unlink(inbox_path)


@pytest.mark.asyncio
async def test_check_inbox_no_path(chat_tools):
    tools, _ = chat_tools
    # _inbox_path is None by default
    result = await tools.check_inbox()
    assert result["count"] == 0
