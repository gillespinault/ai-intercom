"""Tests for Attention Hub models."""

import pytest
from src.shared.models import (
    AttentionState,
    PromptType,
    PromptChoice,
    DetectedPrompt,
    AttentionHeartbeat,
    AttentionSession,
    AttentionEvent,
)


# --- AttentionState enum ---

def test_attention_state_values():
    assert AttentionState.WORKING == "working"
    assert AttentionState.THINKING == "thinking"
    assert AttentionState.WAITING == "waiting"
    assert AttentionState.ENDED == "ended"


def test_attention_state_all_members():
    members = set(AttentionState)
    assert members == {
        AttentionState.WORKING,
        AttentionState.THINKING,
        AttentionState.WAITING,
        AttentionState.ENDED,
    }


# --- PromptType enum ---

def test_prompt_type_values():
    assert PromptType.PERMISSION == "permission"
    assert PromptType.QUESTION == "question"
    assert PromptType.TEXT_INPUT == "text_input"


def test_prompt_type_all_members():
    members = set(PromptType)
    assert members == {
        PromptType.PERMISSION,
        PromptType.QUESTION,
        PromptType.TEXT_INPUT,
    }


# --- PromptChoice ---

def test_prompt_choice():
    choice = PromptChoice(key="y", label="Yes")
    assert choice.key == "y"
    assert choice.label == "Yes"


# --- DetectedPrompt ---

def test_detected_prompt_permission():
    prompt = DetectedPrompt(
        type=PromptType.PERMISSION,
        raw_text="Allow Read tool on /tmp/foo?",
        tool="Read",
        command_preview="/tmp/foo",
        choices=[
            PromptChoice(key="y", label="Yes"),
            PromptChoice(key="n", label="No"),
        ],
    )
    assert prompt.type == PromptType.PERMISSION
    assert prompt.tool == "Read"
    assert prompt.command_preview == "/tmp/foo"
    assert len(prompt.choices) == 2
    assert prompt.choices[0].key == "y"
    assert prompt.allows_free_text is False


def test_detected_prompt_question():
    prompt = DetectedPrompt(
        type=PromptType.QUESTION,
        raw_text="Which branch?",
        question="Which branch should I use?",
        choices=[
            PromptChoice(key="1", label="main"),
            PromptChoice(key="2", label="develop"),
        ],
        allows_free_text=True,
    )
    assert prompt.type == PromptType.QUESTION
    assert prompt.question == "Which branch should I use?"
    assert prompt.allows_free_text is True
    assert len(prompt.choices) == 2


def test_detected_prompt_defaults():
    prompt = DetectedPrompt(type=PromptType.TEXT_INPUT)
    assert prompt.raw_text == ""
    assert prompt.tool is None
    assert prompt.command_preview is None
    assert prompt.question is None
    assert prompt.choices == []
    assert prompt.allows_free_text is False


# --- AttentionHeartbeat ---

def test_attention_heartbeat():
    hb = AttentionHeartbeat(
        pid=12345,
        session_id="s-abc123",
        session_name="Fix bug in router",
        machine="serverlab",
        project="AI-intercom",
        last_tool="Bash",
        last_tool_time="2026-03-01T10:00:00Z",
        tmux_session="claude-0",
        rc_url="http://localhost:7681",
    )
    assert hb.pid == 12345
    assert hb.session_id == "s-abc123"
    assert hb.session_name == "Fix bug in router"
    assert hb.machine == "serverlab"
    assert hb.project == "AI-intercom"
    assert hb.last_tool == "Bash"
    assert hb.last_tool_time == "2026-03-01T10:00:00Z"
    assert hb.tmux_session == "claude-0"
    assert hb.rc_url == "http://localhost:7681"


def test_attention_heartbeat_defaults():
    hb = AttentionHeartbeat(
        pid=1,
        session_id="s-min",
        machine="m1",
        project="p1",
    )
    assert hb.session_name == ""
    assert hb.last_tool == ""
    assert hb.last_tool_time == ""
    assert hb.tmux_session == ""
    assert hb.rc_url is None


# --- AttentionSession ---

def test_attention_session_full():
    session = AttentionSession(
        session_id="s-abc123",
        machine="serverlab",
        project="AI-intercom",
        session_name="Working on models",
        pid=12345,
        state=AttentionState.WAITING,
        state_since="2026-03-01T10:00:00Z",
        last_tool="Bash",
        last_tool_time="2026-03-01T09:59:00Z",
        rc_url="http://localhost:7681",
        idle_seconds=60,
        prompt=DetectedPrompt(
            type=PromptType.PERMISSION,
            raw_text="Allow?",
            tool="Bash",
        ),
        tmux_session="claude-0",
    )
    assert session.session_id == "s-abc123"
    assert session.state == AttentionState.WAITING
    assert session.idle_seconds == 60
    assert session.prompt is not None
    assert session.prompt.type == PromptType.PERMISSION
    assert session.tmux_session == "claude-0"


def test_attention_session_defaults():
    session = AttentionSession(
        session_id="s-min",
        machine="m1",
        project="p1",
        pid=1,
    )
    assert session.state == AttentionState.WORKING
    assert session.state_since == ""
    assert session.last_tool == ""
    assert session.last_tool_time == ""
    assert session.rc_url is None
    assert session.idle_seconds == 0
    assert session.prompt is None
    assert session.session_name == ""
    assert session.tmux_session == ""


# --- AttentionEvent ---

def test_attention_event_with_session():
    session = AttentionSession(
        session_id="s-abc",
        machine="serverlab",
        project="intercom",
        pid=42,
        state=AttentionState.WAITING,
    )
    event = AttentionEvent(
        type="session_update",
        session=session,
    )
    assert event.type == "session_update"
    assert event.session is not None
    assert event.session.session_id == "s-abc"
    assert event.sessions is None
    assert event.timestamp  # non-empty string


def test_attention_event_with_sessions_list():
    s1 = AttentionSession(session_id="s-1", machine="m1", project="p1", pid=1)
    s2 = AttentionSession(session_id="s-2", machine="m2", project="p2", pid=2)
    event = AttentionEvent(
        type="full_state",
        sessions=[s1, s2],
    )
    assert event.type == "full_state"
    assert event.session is None
    assert event.sessions is not None
    assert len(event.sessions) == 2


def test_attention_event_timestamp_auto():
    event = AttentionEvent(type="ping")
    assert event.timestamp  # should be auto-generated ISO string
    assert "T" in event.timestamp  # basic ISO format check
