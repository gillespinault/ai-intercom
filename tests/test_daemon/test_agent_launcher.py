import pytest
from src.daemon.agent_launcher import AgentLauncher


@pytest.fixture
def launcher():
    return AgentLauncher(
        default_command="echo",
        default_args=["test-response"],
        allowed_paths=["/tmp"],
        max_duration=10,
    )


def test_build_prompt_with_context():
    launcher = AgentLauncher("claude", ["-p"], ["/tmp"], 30)
    prompt = launcher.build_prompt(
        mission="Check SSL certs",
        context_messages=[
            {"from": "serverlab/infra", "message": "Please check SSL"},
            {"from": "vps/nginx", "message": "Checking now..."},
        ],
        mission_id="m-test-001",
    )
    assert "m-test-001" in prompt
    assert "Check SSL certs" in prompt
    assert "serverlab/infra" in prompt


def test_build_prompt_no_context():
    launcher = AgentLauncher("claude", ["-p"], ["/tmp"], 30)
    prompt = launcher.build_prompt(
        mission="Do something",
        context_messages=[],
        mission_id="m-test-002",
    )
    assert "Do something" in prompt
    assert "m-test-002" in prompt


def test_validate_path_allowed():
    launcher = AgentLauncher("claude", ["-p"], ["/home/gilles"], 30)
    assert launcher.validate_path("/home/gilles/serverlab") is True


def test_validate_path_rejected():
    launcher = AgentLauncher("claude", ["-p"], ["/home/gilles"], 30)
    assert launcher.validate_path("/etc/shadow") is False
