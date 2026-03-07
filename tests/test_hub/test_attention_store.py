"""Tests for the hub AttentionStore."""

import asyncio
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


class TestWaitingDebounce:
    """Tests for the WAITING notification debounce logic."""

    @pytest.mark.asyncio
    async def test_duplicate_waiting_fires_callback_once(self):
        """Two consecutive WAITING events for the same session only trigger one callback."""
        store = AttentionStore()
        callback = AsyncMock()
        store.set_on_waiting_callback(callback)

        waiting_session = _make_session(state=AttentionState.WAITING)

        store.handle_event("laptop", {
            "type": "new_session",
            "session": waiting_session.model_dump(),
        })
        # Give the asyncio.create_task a chance to run
        await asyncio.sleep(0)

        store.handle_event("laptop", {
            "type": "state_changed",
            "session": waiting_session.model_dump(),
        })
        await asyncio.sleep(0)

        assert callback.call_count == 1

    @pytest.mark.asyncio
    async def test_waiting_working_waiting_fires_twice(self):
        """WAITING → WORKING → WAITING resets debounce, so callback fires twice."""
        store = AttentionStore()
        callback = AsyncMock()
        store.set_on_waiting_callback(callback)

        # First WAITING
        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(state=AttentionState.WAITING).model_dump(),
        })
        await asyncio.sleep(0)

        # WORKING (resets debounce)
        store.handle_event("laptop", {
            "type": "state_changed",
            "session": _make_session(state=AttentionState.WORKING).model_dump(),
        })
        await asyncio.sleep(0)

        # Second WAITING
        store.handle_event("laptop", {
            "type": "state_changed",
            "session": _make_session(state=AttentionState.WAITING).model_dump(),
        })
        await asyncio.sleep(0)

        assert callback.call_count == 2

    @pytest.mark.asyncio
    async def test_session_ended_cleans_debounce(self):
        """session_ended clears the debounce set so a re-created session can notify."""
        store = AttentionStore()
        callback = AsyncMock()
        store.set_on_waiting_callback(callback)

        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(state=AttentionState.WAITING).model_dump(),
        })
        await asyncio.sleep(0)
        assert callback.call_count == 1

        store.handle_event("laptop", {
            "type": "session_ended",
            "session": _make_session(state=AttentionState.ENDED).model_dump(),
        })

        # Same session_id re-appears
        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(state=AttentionState.WAITING).model_dump(),
        })
        await asyncio.sleep(0)
        assert callback.call_count == 2


class TestKeepaliveEvent:
    """Tests for keepalive event handling."""

    def test_keepalive_refreshes_last_update(self):
        """A keepalive event should update last_update without triggering notifications."""
        from datetime import datetime, timedelta, timezone

        store = AttentionStore()

        # Add a session
        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(
                session_id="ka-1", state=AttentionState.WAITING,
            ).model_dump(),
        })
        initial_update = store.get_session("ka-1").last_update

        # Backdate it so we can see the refresh
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=200)
        ).isoformat()
        store._sessions["ka-1"].last_update = old_time

        # Send keepalive
        store.handle_event("laptop", {
            "type": "keepalive",
            "session": _make_session(
                session_id="ka-1", state=AttentionState.WAITING,
            ).model_dump(),
        })

        refreshed = store.get_session("ka-1")
        assert refreshed is not None
        assert refreshed.last_update != old_time  # Was refreshed

    @pytest.mark.asyncio
    async def test_keepalive_does_not_trigger_notification(self):
        """Keepalive should not trigger the on_waiting callback."""
        store = AttentionStore()
        callback = AsyncMock()
        store.set_on_waiting_callback(callback)

        # Initial new_session in WAITING triggers callback
        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(
                session_id="ka-2", state=AttentionState.WAITING,
            ).model_dump(),
        })
        await asyncio.sleep(0)
        assert callback.call_count == 1

        # Keepalive should NOT trigger again
        store.handle_event("laptop", {
            "type": "keepalive",
            "session": _make_session(
                session_id="ka-2", state=AttentionState.WAITING,
            ).model_dump(),
        })
        await asyncio.sleep(0)
        assert callback.call_count == 1  # Still 1


class TestStaleSessionCleanup:
    """Tests for automatic cleanup of stale sessions."""

    @pytest.mark.asyncio
    async def test_stale_session_removed(self):
        """Sessions with last_update older than threshold are removed."""
        from datetime import datetime, timedelta, timezone
        from src.hub.attention_store import STALE_TIMEOUT_SECONDS

        store = AttentionStore()

        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(session_id="stale-1").model_dump(),
        })
        assert len(store.get_all_sessions()) == 1

        # Simulate time passing by backdating last_update
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=STALE_TIMEOUT_SECONDS + 60)
        ).isoformat()
        store._sessions["stale-1"].last_update = old_time

        await store._cleanup_stale_sessions()
        assert len(store.get_all_sessions()) == 0

    @pytest.mark.asyncio
    async def test_fresh_session_kept(self):
        """Sessions with recent last_update are not removed."""
        store = AttentionStore()

        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(session_id="fresh-1").model_dump(),
        })
        assert len(store.get_all_sessions()) == 1

        # last_update is set to now() by handle_event, so it should be fresh
        await store._cleanup_stale_sessions()
        assert len(store.get_all_sessions()) == 1

    @pytest.mark.asyncio
    async def test_mixed_stale_and_fresh(self):
        """Only stale sessions are removed; fresh ones remain."""
        from datetime import datetime, timedelta, timezone
        from src.hub.attention_store import STALE_TIMEOUT_SECONDS

        store = AttentionStore()

        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(session_id="stale", pid=1111).model_dump(),
        })
        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(session_id="fresh", pid=2222).model_dump(),
        })
        assert len(store.get_all_sessions()) == 2

        # Backdate only the stale session
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=STALE_TIMEOUT_SECONDS + 60)
        ).isoformat()
        store._sessions["stale"].last_update = old_time

        await store._cleanup_stale_sessions()
        assert len(store.get_all_sessions()) == 1
        assert store.get_session("fresh") is not None
        assert store.get_session("stale") is None

    @pytest.mark.asyncio
    async def test_cleanup_clears_waiting_debounce(self):
        """Stale session cleanup also clears the waiting notification debounce."""
        from datetime import datetime, timedelta, timezone
        from src.hub.attention_store import STALE_TIMEOUT_SECONDS

        store = AttentionStore()
        callback = AsyncMock()
        store.set_on_waiting_callback(callback)

        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(
                session_id="stale-wait", state=AttentionState.WAITING
            ).model_dump(),
        })
        await asyncio.sleep(0)
        assert callback.call_count == 1
        assert "stale-wait" in store._notified_waiting

        # Backdate the session
        old_time = (
            datetime.now(timezone.utc) - timedelta(seconds=STALE_TIMEOUT_SECONDS + 60)
        ).isoformat()
        store._sessions["stale-wait"].last_update = old_time

        await store._cleanup_stale_sessions()
        assert "stale-wait" not in store._notified_waiting


class TestNotificationPrefs:
    """Tests for Telegram notification preference filtering."""

    def test_default_prefs_all_enabled(self, tmp_path):
        store = AttentionStore(prefs_path=str(tmp_path / "prefs.json"))
        prefs = store.get_notification_prefs()
        assert prefs == {"permission": True, "question": True, "text_input": True}

    def test_update_prefs_partial(self, tmp_path):
        store = AttentionStore(prefs_path=str(tmp_path / "prefs.json"))
        store.update_notification_prefs({"question": False})
        prefs = store.get_notification_prefs()
        assert prefs["question"] is False
        assert prefs["permission"] is True  # unchanged

    def test_update_prefs_ignores_unknown_keys(self, tmp_path):
        store = AttentionStore(prefs_path=str(tmp_path / "prefs.json"))
        store.update_notification_prefs({"unknown_key": True, "permission": False})
        prefs = store.get_notification_prefs()
        assert prefs["permission"] is False
        assert "unknown_key" not in prefs

    def test_should_notify_telegram_respects_prefs(self, tmp_path):
        store = AttentionStore(prefs_path=str(tmp_path / "prefs.json"))
        store.update_notification_prefs({"question": False})
        assert store.should_notify_telegram("permission") is True
        assert store.should_notify_telegram("question") is False
        assert store.should_notify_telegram("text_input") is True

    def test_should_notify_telegram_unknown_type_defaults_true(self):
        store = AttentionStore()
        assert store.should_notify_telegram("unknown") is True

    def test_prefs_persistence(self, tmp_path):
        prefs_file = tmp_path / "notification_prefs.json"
        store = AttentionStore(prefs_path=str(prefs_file))
        store.update_notification_prefs({"question": False})

        # New store loads persisted prefs
        store2 = AttentionStore(prefs_path=str(prefs_file))
        assert store2.get_notification_prefs()["question"] is False


class TestNotificationPrefsFiltering:
    """Tests that notification prefs filter Telegram callbacks."""

    @pytest.mark.asyncio
    async def test_waiting_callback_skipped_when_type_disabled(self):
        """If question prefs is False, callback is NOT called for question prompts."""
        store = AttentionStore()
        callback = AsyncMock()
        store.set_on_waiting_callback(callback)
        store.update_notification_prefs({"question": False})

        session_data = _make_session(state=AttentionState.WAITING).model_dump()
        session_data["prompt"] = {"type": "question", "raw_text": "Choose one"}

        store.handle_event("laptop", {"type": "state_changed", "session": session_data})
        await asyncio.sleep(0)

        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_waiting_callback_called_when_type_enabled(self):
        """If permission prefs is True, callback IS called for permission prompts."""
        store = AttentionStore()
        callback = AsyncMock()
        store.set_on_waiting_callback(callback)
        # permission is True by default

        session_data = _make_session(state=AttentionState.WAITING).model_dump()
        session_data["prompt"] = {"type": "permission", "raw_text": "Allow Bash?", "tool": "Bash"}

        store.handle_event("laptop", {"type": "state_changed", "session": session_data})
        await asyncio.sleep(0)

        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_waiting_callback_called_when_no_prompt(self):
        """Sessions in WAITING without a prompt always trigger callback."""
        store = AttentionStore()
        callback = AsyncMock()
        store.set_on_waiting_callback(callback)

        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(state=AttentionState.WAITING).model_dump(),
        })
        await asyncio.sleep(0)

        callback.assert_called_once()


from src.shared.models import PermissionRequest, PermissionDecision


class TestPendingPermissions:
    def test_add_pending_permission(self):
        store = AttentionStore()
        req = PermissionRequest(
            session_id="sess-1",
            tool_name="Bash",
            tool_input={"command": "docker ps"},
            machine="laptop",
        )
        store.add_pending_permission(req)
        assert store.get_pending_permission(req.request_id) is not None

    def test_resolve_permission_allow(self):
        store = AttentionStore()
        req = PermissionRequest(
            session_id="sess-1",
            tool_name="Bash",
            tool_input={"command": "docker ps"},
            machine="laptop",
        )
        store.add_pending_permission(req)
        decision = PermissionDecision(behavior="allow")
        store.resolve_permission(req.request_id, decision)
        assert store.get_pending_permission(req.request_id) is None

    def test_resolve_triggers_callback(self):
        store = AttentionStore()
        req = PermissionRequest(
            session_id="sess-1",
            tool_name="Bash",
            tool_input={"command": "docker ps"},
            machine="laptop",
        )
        store.add_pending_permission(req)

        resolved = None
        def on_resolve(request_id, decision):
            nonlocal resolved
            resolved = (request_id, decision)

        store.set_on_permission_resolved(on_resolve)
        decision = PermissionDecision(behavior="deny", reason="nope")
        store.resolve_permission(req.request_id, decision)
        assert resolved is not None
        assert resolved[1].behavior == "deny"

    def test_get_pending_permission_not_found(self):
        store = AttentionStore()
        assert store.get_pending_permission("nonexistent") is None

    def test_list_pending_permissions(self):
        store = AttentionStore()
        req1 = PermissionRequest(session_id="s1", tool_name="Bash", tool_input={}, machine="m1")
        req2 = PermissionRequest(session_id="s2", tool_name="Read", tool_input={}, machine="m1")
        store.add_pending_permission(req1)
        store.add_pending_permission(req2)
        assert len(store.list_pending_permissions()) == 2

    def test_expire_old_permissions(self):
        store = AttentionStore()
        req = PermissionRequest(
            session_id="sess-1",
            tool_name="Bash",
            tool_input={},
            machine="laptop",
            created_at="2020-01-01T00:00:00+00:00",
        )
        store.add_pending_permission(req)
        expired = store.expire_permissions(max_age_seconds=60)
        assert len(expired) == 1
        assert store.get_pending_permission(req.request_id) is None
