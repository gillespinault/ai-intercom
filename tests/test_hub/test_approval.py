import pytest
from src.hub.approval import ApprovalEngine, ApprovalLevel
from src.shared.models import Message, MessageType


@pytest.fixture
def engine():
    policies = {
        "defaults": {"require_approval": "once"},
        "rules": [
            {
                "from": "*",
                "to": "*",
                "type": "ask",
                "message_pattern": "check|status|verify",
                "approval": "never",
                "label": "Read-only",
            },
            {
                "from": "serverlab/*",
                "to": "vps/nginx",
                "type": "ask",
                "approval": "always_allow",
                "label": "Serverlab to VPS",
            },
            {
                "from": "*",
                "to": "*",
                "type": "start_agent",
                "approval": "once",
                "label": "Agent launch",
            },
        ],
    }
    return ApprovalEngine(policies)


def test_read_only_auto_approved(engine):
    msg = Message(
        from_agent="jetson/mnemos",
        to_agent="vps/nginx",
        type=MessageType.ASK,
        payload={"message": "check SSL status"},
    )
    result = engine.evaluate(msg)
    assert result == ApprovalLevel.NEVER


def test_serverlab_to_vps_always_allowed(engine):
    msg = Message(
        from_agent="serverlab/infra",
        to_agent="vps/nginx",
        type=MessageType.ASK,
        payload={"message": "add reverse proxy"},
    )
    result = engine.evaluate(msg)
    assert result == ApprovalLevel.ALWAYS_ALLOW


def test_start_agent_needs_approval(engine):
    msg = Message(
        from_agent="serverlab/infra",
        to_agent="vps/nginx",
        type=MessageType.START_AGENT,
        payload={"message": "launch agent"},
    )
    result = engine.evaluate(msg)
    assert result == ApprovalLevel.ONCE


def test_default_policy_applied(engine):
    msg = Message(
        from_agent="laptop/app",
        to_agent="jetson/mnemos",
        type=MessageType.ASK,
        payload={"message": "deploy new model"},
    )
    result = engine.evaluate(msg)
    assert result == ApprovalLevel.ONCE


def test_grant_mission_approval(engine):
    msg = Message(
        from_agent="laptop/app",
        to_agent="jetson/mnemos",
        type=MessageType.ASK,
        payload={"message": "deploy"},
        mission_id="m-test-001",
    )
    engine.grant("m-test-001", "laptop/app", "jetson/mnemos", ApprovalLevel.MISSION)
    result = engine.evaluate(msg)
    assert result == ApprovalLevel.MISSION
