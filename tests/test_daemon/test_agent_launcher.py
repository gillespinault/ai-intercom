import asyncio

import pytest
from src.daemon.agent_launcher import AgentLauncher, MissionResult


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


def test_mission_result_defaults():
    result = MissionResult()
    assert result.status == "running"
    assert result.output is None
    assert result.finished_at is None


async def test_launch_background_returns_immediately(launcher):
    """launch_background should return the mission_id without waiting."""
    mission_id = await launcher.launch_background(
        mission="hello",
        context_messages=[],
        mission_id="bg-001",
        project_path="/tmp",
    )
    assert mission_id == "bg-001"
    # Status should be running immediately after launch
    result = launcher.get_status("bg-001")
    assert result is not None
    assert result.started_at != ""
    # Wait for completion to avoid orphan tasks
    await asyncio.sleep(1)


async def test_launch_background_completes(launcher):
    """Background task should complete and store output."""
    await launcher.launch_background(
        mission="test",
        context_messages=[],
        mission_id="bg-002",
        project_path="/tmp",
    )
    # Wait for the background task to finish
    await asyncio.sleep(2)
    result = launcher.get_status("bg-002")
    assert result is not None
    assert result.status == "completed"
    assert result.output is not None
    assert result.finished_at is not None


async def test_launch_background_invalid_path(launcher):
    """Background launch with invalid path should fail immediately."""
    await launcher.launch_background(
        mission="test",
        context_messages=[],
        mission_id="bg-003",
        project_path="/etc/shadow",
    )
    result = launcher.get_status("bg-003")
    assert result is not None
    assert result.status == "failed"
    assert "not in allowed_paths" in (result.output or "")


async def test_get_status_unknown():
    launcher = AgentLauncher("echo", [], ["/tmp"], 10)
    assert launcher.get_status("nonexistent") is None


async def test_stop_running_process():
    """Stopping a running process should kill it and mark as failed."""
    launcher = AgentLauncher(
        default_command="bash",
        default_args=["-c", "sleep 5; echo done"],
        allowed_paths=["/tmp"],
        max_duration=60,
    )
    await launcher.launch_background(
        mission="long task",
        context_messages=[],
        mission_id="bg-stop",
        project_path="/tmp",
    )
    await asyncio.sleep(0.3)
    stopped = await launcher.stop("bg-stop")
    assert stopped is True
    # Give _run_agent a moment to process the kill result
    await asyncio.sleep(0.5)
    result = launcher.get_status("bg-stop")
    assert result is not None
    assert result.status == "failed"
