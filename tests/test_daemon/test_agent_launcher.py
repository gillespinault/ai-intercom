import asyncio
import json

import pytest
from src.daemon.agent_launcher import (
    AgentLauncher,
    FeedbackItem,
    MissionResult,
    _summarize_tool_input,
)


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


# --- Streaming feedback tests ---


def test_feedback_item_creation():
    """FeedbackItem should store timestamp, kind, and summary."""
    fb = FeedbackItem(timestamp="2026-02-28T09:00:00Z", kind="tool", summary="\U0001f4d6 Lecture de config.py")
    assert fb.kind == "tool"
    assert "\U0001f4d6" in fb.summary
    assert fb.timestamp == "2026-02-28T09:00:00Z"


def test_mission_result_with_feedback():
    """MissionResult should have empty feedback list and zero turn_count by default."""
    result = MissionResult()
    assert result.feedback == []
    assert result.turn_count == 0


def test_mission_result_defaults_preserved():
    """Existing defaults should still work with new fields."""
    result = MissionResult()
    assert result.status == "running"
    assert result.output is None
    assert result.finished_at is None
    assert result.feedback == []
    assert result.turn_count == 0


def test_process_stream_line_tool_use():
    """_process_stream_line should create FeedbackItem for tool_use blocks."""
    launcher = AgentLauncher("echo", [], ["/tmp"], 10)
    mission_id = "stream-001"
    launcher._results[mission_id] = MissionResult(started_at="now")

    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": "/home/gilles/serverlab/src/config.py"},
                }
            ]
        },
    }
    result = launcher._process_stream_line(json.dumps(event), mission_id)
    assert result is None  # Not a result event

    mr = launcher._results[mission_id]
    assert len(mr.feedback) == 1
    assert mr.feedback[0].kind == "tool"
    assert "\U0001f4d6" in mr.feedback[0].summary
    assert "src/config.py" in mr.feedback[0].summary
    assert mr.turn_count == 1


def test_process_stream_line_text():
    """_process_stream_line should create FeedbackItem for long text blocks."""
    launcher = AgentLauncher("echo", [], ["/tmp"], 10)
    mission_id = "stream-002"
    launcher._results[mission_id] = MissionResult(started_at="now")

    event = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "This is a long response with detailed explanation of the changes made."}
            ]
        },
    }
    launcher._process_stream_line(json.dumps(event), mission_id)

    mr = launcher._results[mission_id]
    assert len(mr.feedback) == 1
    assert mr.feedback[0].kind == "text"
    assert "\U0001f4ac" in mr.feedback[0].summary


def test_process_stream_line_short_text_ignored():
    """Short text blocks (<= 20 chars) should not create feedback."""
    launcher = AgentLauncher("echo", [], ["/tmp"], 10)
    mission_id = "stream-003"
    launcher._results[mission_id] = MissionResult(started_at="now")

    event = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "OK"}]},
    }
    launcher._process_stream_line(json.dumps(event), mission_id)

    mr = launcher._results[mission_id]
    # turn_count incremented but no feedback for short text
    assert mr.turn_count == 1
    assert len(mr.feedback) == 0


def test_process_stream_line_invalid_json():
    """Invalid JSON lines should be silently ignored."""
    launcher = AgentLauncher("echo", [], ["/tmp"], 10)
    mission_id = "stream-004"
    launcher._results[mission_id] = MissionResult(started_at="now")

    result = launcher._process_stream_line("not valid json {{{", mission_id)
    assert result is None
    assert len(launcher._results[mission_id].feedback) == 0


def test_process_stream_line_result():
    """Result events should return the final output text."""
    launcher = AgentLauncher("echo", [], ["/tmp"], 10)
    mission_id = "stream-005"
    launcher._results[mission_id] = MissionResult(started_at="now")

    event = {"type": "result", "result": "All tasks completed successfully."}
    output = launcher._process_stream_line(json.dumps(event), mission_id)
    assert output == "All tasks completed successfully."


def test_summarize_tool_input_read():
    """_summarize_tool_input should extract last 2 path segments for Read."""
    result = _summarize_tool_input("Read", {"file_path": "/home/gilles/serverlab/src/config.py"})
    assert result == "src/config.py"


def test_summarize_tool_input_bash():
    """_summarize_tool_input should truncate long bash commands."""
    cmd = "docker compose -f /home/gilles/serverlab/services/docker-compose.yml up -d --build --no-cache"
    result = _summarize_tool_input("Bash", {"command": cmd})
    assert len(result) <= 83  # 80 + "..."
    assert result.startswith("docker compose")


def test_summarize_tool_input_grep():
    """_summarize_tool_input should return pattern for Grep."""
    result = _summarize_tool_input("Grep", {"pattern": "def main"})
    assert result == "def main"


def test_summarize_tool_input_agent():
    """_summarize_tool_input should return description for Agent."""
    result = _summarize_tool_input("Agent", {"description": "Explore codebase"})
    assert result == "Explore codebase"


def test_summarize_tool_input_unknown():
    """Unknown tools should return empty string."""
    result = _summarize_tool_input("UnknownTool", {"foo": "bar"})
    assert result == ""
