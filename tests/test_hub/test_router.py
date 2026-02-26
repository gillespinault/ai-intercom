import pytest
from unittest.mock import AsyncMock
from src.hub.router import Router
from src.shared.models import Message, MessageType


@pytest.fixture
def mock_registry():
    reg = AsyncMock()
    reg.get_machine.return_value = {
        "id": "vps",
        "daemon_url": "http://100.75.129.81:7700",
        "token": "ict_vps_test",
        "status": "online",
    }
    return reg


@pytest.fixture
def mock_approval():
    from src.hub.approval import ApprovalEngine
    engine = ApprovalEngine({"defaults": {"require_approval": "never"}, "rules": []})
    return engine


@pytest.fixture
def router(mock_registry, mock_approval):
    return Router(
        registry=mock_registry,
        approval_engine=mock_approval,
        send_to_daemon=AsyncMock(return_value={"status": "received"}),
        send_telegram=AsyncMock(),
        request_approval=AsyncMock(),
    )


async def test_route_message_to_online_daemon(router):
    msg = Message(
        from_agent="serverlab/infra",
        to_agent="vps/nginx",
        type=MessageType.ASK,
        payload={"message": "hello"},
    )
    result = await router.route(msg)
    assert result["status"] == "received"
    router.send_to_daemon.assert_called_once()
    router.send_telegram.assert_called_once()


async def test_route_to_offline_machine(router, mock_registry):
    mock_registry.get_machine.return_value = {
        "id": "jetson",
        "daemon_url": "http://100.79.231.116:7700",
        "token": "tok",
        "status": "offline",
    }
    msg = Message(
        from_agent="serverlab/infra",
        to_agent="jetson/mnemos",
        type=MessageType.ASK,
        payload={"message": "hello"},
    )
    result = await router.route(msg)
    assert result["status"] == "error"
    assert "offline" in result["error"]


async def test_route_message_posted_to_telegram(router):
    msg = Message(
        from_agent="serverlab/infra",
        to_agent="vps/nginx",
        type=MessageType.SEND,
        payload={"message": "notification"},
    )
    await router.route(msg)
    router.send_telegram.assert_called_once()
