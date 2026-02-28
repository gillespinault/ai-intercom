import pytest
from src.shared.models import (
    AgentId, Message, MessageType, AgentInfo, AgentStatus, MachineInfo,
    SessionInfo, ThreadMessage,
)


def test_agent_id_from_string():
    aid = AgentId.from_string("vps/nginx")
    assert aid.machine == "vps"
    assert aid.project == "nginx"
    assert str(aid) == "vps/nginx"


def test_agent_id_invalid():
    with pytest.raises(ValueError):
        AgentId.from_string("invalid")


def test_agent_id_empty_parts():
    with pytest.raises(ValueError):
        AgentId.from_string("/project")
    with pytest.raises(ValueError):
        AgentId.from_string("machine/")


def test_message_creation():
    msg = Message(
        from_agent="serverlab/infra",
        to_agent="vps/nginx",
        type=MessageType.ASK,
        payload={"message": "hello"},
    )
    assert msg.id is not None
    assert msg.mission_id is not None
    assert msg.timestamp is not None
    assert msg.version == "1"


def test_message_with_existing_mission():
    msg = Message(
        from_agent="serverlab/infra",
        to_agent="vps/nginx",
        type=MessageType.RESPONSE,
        payload={"message": "done"},
        mission_id="m-existing",
    )
    assert msg.mission_id == "m-existing"


def test_message_human_agent():
    msg = Message(
        from_agent="human",
        to_agent="serverlab/infra",
        type=MessageType.ASK,
        payload={"message": "status?"},
    )
    assert msg.from_agent == "human"


def test_message_invalid_agent_id():
    with pytest.raises(Exception):
        Message(
            from_agent="badid",
            to_agent="vps/nginx",
            type=MessageType.ASK,
            payload={},
        )


def test_agent_info():
    agent = AgentInfo(
        machine="vps",
        project="nginx",
        description="Reverse proxy",
        capabilities=["nginx", "ssl"],
        agent_command="claude",
    )
    assert agent.id == "vps/nginx"
    assert agent.status == AgentStatus.UNKNOWN


def test_machine_info():
    machine = MachineInfo(
        id="vps",
        display_name="VPS Hostinger",
        tailscale_ip="100.75.129.81",
        daemon_url="http://100.75.129.81:7700",
    )
    assert machine.id == "vps"
    assert machine.display_name == "VPS Hostinger"
    assert machine.status == AgentStatus.UNKNOWN
    assert machine.projects == []


def test_message_type_values():
    assert MessageType.ASK == "ask"
    assert MessageType.SEND == "send"
    assert MessageType.RESPONSE == "response"
    assert MessageType.START_AGENT == "start_agent"
    assert MessageType.STATUS == "status"


def test_message_type_chat():
    assert MessageType.CHAT == "chat"


def test_session_info_defaults():
    s = SessionInfo(session_id="s-123", project="myproj", pid=999, inbox_path="/tmp/inbox.jsonl")
    assert s.status == "active"
    assert s.summary == ""
    assert s.recent_activity == []
    assert s.registered_at == ""


def test_thread_message_defaults():
    m = ThreadMessage(
        thread_id="t-abc",
        from_agent="limn/mnemos",
        timestamp="2026-02-28T16:00:00Z",
        message="hello",
    )
    assert m.read is False


def test_thread_message_read():
    m = ThreadMessage(
        thread_id="t-abc",
        from_agent="limn/mnemos",
        timestamp="2026-02-28T16:00:00Z",
        message="hello",
        read=True,
    )
    assert m.read is True
