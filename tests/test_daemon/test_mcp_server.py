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


async def test_ask_message(tools):
    tools.hub_client.ask.return_value = {
        "status": "completed",
        "response": "done",
        "mission_id": "m-001",
    }
    result = await tools.ask(to="vps/nginx", message="do something", timeout=60)
    assert result["status"] == "completed"
    assert result["response"] == "done"


async def test_register_update(tools):
    tools.hub_client.register.return_value = {"status": "updated"}
    result = await tools.register(
        action="update",
        project={"description": "Updated desc", "capabilities": ["new"]},
    )
    assert result["status"] == "updated"
