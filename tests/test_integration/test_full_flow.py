import pytest
from src.hub.registry import Registry
from src.hub.approval import ApprovalEngine
from src.hub.router import Router
from src.shared.models import Message, MessageType
from unittest.mock import AsyncMock


@pytest.fixture
async def registry(tmp_path):
    reg = Registry(str(tmp_path / "test.db"))
    await reg.init()
    # Pre-register a machine
    await reg.register_machine("vps", "VPS", "127.0.0.1", "http://127.0.0.1:7701", "vps-token")
    await reg.register_project("vps", "nginx", "Reverse proxy", ["nginx"], "/etc/nginx")
    yield reg
    await reg.close()


@pytest.fixture
def approval():
    return ApprovalEngine({"defaults": {"require_approval": "never"}, "rules": []})


async def test_end_to_end_message_flow(registry, approval):
    """Test: agent sends message -> hub routes -> daemon receives."""
    daemon_received = []

    async def mock_send_to_daemon(url, message, token):
        daemon_received.append(message)
        return {"status": "received", "mission_id": message.get("mission_id")}

    router = Router(
        registry=registry,
        approval_engine=approval,
        send_to_daemon=mock_send_to_daemon,
        send_telegram=AsyncMock(),
        request_approval=AsyncMock(),
    )

    msg = Message(
        from_agent="serverlab/infra",
        to_agent="vps/nginx",
        type=MessageType.ASK,
        payload={"message": "Add reverse proxy for test.example.com"},
    )

    result = await router.route(msg)
    assert result["status"] == "received"
    assert len(daemon_received) == 1
    assert daemon_received[0]["to_agent"] == "vps/nginx"


async def test_offline_machine_rejected(registry, approval):
    """Test: message to offline machine is rejected."""
    await registry.register_machine("jetson", "Jetson", "1.2.3.4", "http://1.2.3.4:7700", "tok")
    # Force offline status
    import aiosqlite
    async with aiosqlite.connect(registry._db_path) as db:
        await db.execute("UPDATE machines SET status = 'offline' WHERE id = 'jetson'")
        await db.commit()

    router = Router(
        registry=registry,
        approval_engine=approval,
        send_to_daemon=AsyncMock(),
        send_telegram=AsyncMock(),
        request_approval=AsyncMock(),
    )

    msg = Message(
        from_agent="serverlab/infra",
        to_agent="jetson/mnemos",
        type=MessageType.ASK,
        payload={"message": "test"},
    )

    result = await router.route(msg)
    assert result["status"] == "error"
    assert "offline" in result["error"]
