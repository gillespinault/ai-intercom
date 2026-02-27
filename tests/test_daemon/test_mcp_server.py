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
