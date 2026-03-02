"""Tests for the hub AttentionStore."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.hub.attention_store import AttentionStore
from src.shared.models import (
    AttentionSession,
    AttentionState,
    DetectedPrompt,
    PromptType,
)


def _make_session(
    session_id: str = "sess-1",
    machine: str = "laptop",
    project: str = "my-project",
    state: AttentionState = AttentionState.WORKING,
    pid: int = 1234,
    tmux_session: str = "claude-1234",
) -> AttentionSession:
    return AttentionSession(
        session_id=session_id,
        machine=machine,
        project=project,
        pid=pid,
        state=state,
        state_since="2025-01-01T00:00:00+00:00",
        tmux_session=tmux_session,
    )


class TestHandleNewSession:
    def test_new_session_is_stored(self):
        store = AttentionStore()
        session = _make_session()

        store.handle_event("laptop", {
            "type": "new_session",
            "session": session.model_dump(),
        })

        assert len(store.get_all_sessions()) == 1
        stored = store.get_session("sess-1")
        assert stored is not None
        assert stored.session_id == "sess-1"
        assert stored.machine == "laptop"
        assert stored.state == AttentionState.WORKING

    def test_new_session_accepts_model_instance(self):
        store = AttentionStore()
        session = _make_session()

        store.handle_event("laptop", {
            "type": "new_session",
            "session": session,
        })

        assert store.get_session("sess-1") is not None


class TestHandleStateChanged:
    def test_state_changed_updates_session(self):
        store = AttentionStore()

        # Add initial session
        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(state=AttentionState.WORKING).model_dump(),
        })

        # Update state
        store.handle_event("laptop", {
            "type": "state_changed",
            "session": _make_session(state=AttentionState.WAITING).model_dump(),
        })

        session = store.get_session("sess-1")
        assert session is not None
        assert session.state == AttentionState.WAITING

    def test_state_changed_with_prompt(self):
        store = AttentionStore()
        session_data = _make_session(state=AttentionState.WAITING).model_dump()
        session_data["prompt"] = {
            "type": "permission",
            "raw_text": "Allow Bash?",
            "tool": "Bash",
        }

        store.handle_event("laptop", {
            "type": "state_changed",
            "session": session_data,
        })

        session = store.get_session("sess-1")
        assert session is not None
        assert session.prompt is not None
        assert session.prompt.type == PromptType.PERMISSION


class TestHandleSessionEnded:
    def test_session_ended_removes_session(self):
        store = AttentionStore()

        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session().model_dump(),
        })
        assert len(store.get_all_sessions()) == 1

        store.handle_event("laptop", {
            "type": "session_ended",
            "session": _make_session(state=AttentionState.ENDED).model_dump(),
        })
        assert len(store.get_all_sessions()) == 0
        assert store.get_session("sess-1") is None

    def test_session_ended_nonexistent_is_noop(self):
        store = AttentionStore()
        # Should not raise
        store.handle_event("laptop", {
            "type": "session_ended",
            "session": _make_session(session_id="nonexistent").model_dump(),
        })
        assert len(store.get_all_sessions()) == 0


class TestMultipleMachines:
    def test_sessions_from_different_machines(self):
        store = AttentionStore()

        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(
                session_id="sess-laptop",
                machine="laptop",
                project="proj-a",
            ).model_dump(),
        })

        store.handle_event("vps", {
            "type": "new_session",
            "session": _make_session(
                session_id="sess-vps",
                machine="vps",
                project="proj-b",
                pid=5678,
            ).model_dump(),
        })

        all_sessions = store.get_all_sessions()
        assert len(all_sessions) == 2

        machines = {s.machine for s in all_sessions}
        assert machines == {"laptop", "vps"}

    def test_ending_one_machine_preserves_other(self):
        store = AttentionStore()

        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(session_id="sess-a", machine="laptop").model_dump(),
        })
        store.handle_event("vps", {
            "type": "new_session",
            "session": _make_session(session_id="sess-b", machine="vps").model_dump(),
        })

        store.handle_event("laptop", {
            "type": "session_ended",
            "session": _make_session(session_id="sess-a", machine="laptop").model_dump(),
        })

        assert len(store.get_all_sessions()) == 1
        assert store.get_session("sess-b") is not None


class TestGetWaitingSessions:
    def test_returns_only_waiting(self):
        store = AttentionStore()

        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(
                session_id="working-1", state=AttentionState.WORKING,
            ).model_dump(),
        })
        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(
                session_id="thinking-1", state=AttentionState.THINKING, pid=2222,
            ).model_dump(),
        })
        store.handle_event("vps", {
            "type": "new_session",
            "session": _make_session(
                session_id="waiting-1", machine="vps", state=AttentionState.WAITING, pid=3333,
            ).model_dump(),
        })
        store.handle_event("vps", {
            "type": "new_session",
            "session": _make_session(
                session_id="waiting-2", machine="vps", state=AttentionState.WAITING, pid=4444,
            ).model_dump(),
        })

        waiting = store.get_waiting_sessions()
        assert len(waiting) == 2
        ids = {s.session_id for s in waiting}
        assert ids == {"waiting-1", "waiting-2"}

    def test_empty_when_none_waiting(self):
        store = AttentionStore()

        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(state=AttentionState.WORKING).model_dump(),
        })

        assert store.get_waiting_sessions() == []


class TestGetSessionById:
    def test_existing_session(self):
        store = AttentionStore()
        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(session_id="target").model_dump(),
        })

        result = store.get_session("target")
        assert result is not None
        assert result.session_id == "target"

    def test_nonexistent_session(self):
        store = AttentionStore()
        assert store.get_session("does-not-exist") is None


class TestSubscribeUnsubscribe:
    def test_subscribe_and_unsubscribe(self):
        store = AttentionStore()
        ws = MagicMock()

        store.subscribe(ws)
        assert ws in store._subscribers

        store.unsubscribe(ws)
        assert ws not in store._subscribers

    def test_unsubscribe_nonexistent_is_noop(self):
        store = AttentionStore()
        ws = MagicMock()
        # Should not raise
        store.unsubscribe(ws)


class TestBroadcast:
    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all(self):
        store = AttentionStore()

        ws1 = AsyncMock()
        ws2 = AsyncMock()
        store.subscribe(ws1)
        store.subscribe(ws2)

        event = {"type": "new_session", "session_id": "s1"}
        await store.broadcast(event)

        expected = json.dumps(event)
        ws1.send_text.assert_called_once_with(expected)
        ws2.send_text.assert_called_once_with(expected)

    @pytest.mark.asyncio
    async def test_broadcast_removes_dead_connections(self):
        store = AttentionStore()

        ws_alive = AsyncMock()
        ws_dead = AsyncMock()
        ws_dead.send_text.side_effect = Exception("connection closed")

        store.subscribe(ws_alive)
        store.subscribe(ws_dead)

        await store.broadcast({"type": "test"})

        # Dead connection removed
        assert ws_dead not in store._subscribers
        # Alive connection remains
        assert ws_alive in store._subscribers


class TestUnknownEventType:
    def test_unknown_type_is_ignored(self):
        store = AttentionStore()
        store.handle_event("laptop", {
            "type": "unknown_event",
            "session": _make_session().model_dump(),
        })
        # No sessions added
        assert len(store.get_all_sessions()) == 0

    def test_missing_session_data_is_ignored(self):
        store = AttentionStore()
        store.handle_event("laptop", {"type": "new_session"})
        assert len(store.get_all_sessions()) == 0
