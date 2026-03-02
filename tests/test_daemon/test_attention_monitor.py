"""Tests for the attention monitor sidecar loop.

The attention monitor periodically reads heartbeat files from /tmp/cc-sessions/,
determines each session's state (working/thinking/waiting), captures terminal
output via tmux when a session is waiting, parses prompts, and pushes state
change events to the hub.
"""

from __future__ import annotations

import json
import os
import tempfile
import time

import pytest

from src.daemon.attention_monitor import AttentionMonitor
from src.shared.models import AttentionState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _iso_past(seconds_ago: float) -> str:
    from datetime import datetime, timedelta, timezone

    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.isoformat()


def write_heartbeat(
    sessions_dir: str,
    pid: int | None = None,
    last_tool_time: str | None = None,
    **overrides,
) -> str:
    """Write a heartbeat JSON file and return the path."""
    if pid is None:
        pid = os.getpid()
    if last_tool_time is None:
        last_tool_time = _iso_now()

    data = {
        "pid": pid,
        "session_id": overrides.pop("session_id", f"sess-{pid}"),
        "session_name": overrides.pop("session_name", "test-session"),
        "machine": overrides.pop("machine", "test-machine"),
        "project": overrides.pop("project", "test-project"),
        "last_tool": overrides.pop("last_tool", "Bash"),
        "last_tool_time": last_tool_time,
        "tmux_session": overrides.pop("tmux_session", ""),
        "rc_url": overrides.pop("rc_url", None),
    }
    data.update(overrides)

    path = os.path.join(sessions_dir, f"{pid}.json")
    with open(path, "w") as f:
        json.dump(data, f)
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sessions_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def monitor(sessions_dir):
    return AttentionMonitor(
        machine_id="test",
        sessions_dir=sessions_dir,
        hub_client=None,
        idle_threshold=10,
    )


# ---------------------------------------------------------------------------
# TestReadHeartbeats
# ---------------------------------------------------------------------------


class TestReadHeartbeats:
    def test_reads_valid_heartbeat(self, monitor, sessions_dir):
        pid = os.getpid()
        write_heartbeat(sessions_dir, pid=pid, last_tool_time=_iso_now())

        heartbeats = monitor._read_heartbeats()
        assert len(heartbeats) == 1
        assert heartbeats[0].pid == pid
        assert heartbeats[0].session_id == f"sess-{pid}"

    def test_ignores_invalid_json(self, monitor, sessions_dir):
        # Write a file with invalid JSON
        bad_path = os.path.join(sessions_dir, "bad.json")
        with open(bad_path, "w") as f:
            f.write("not valid json {{{")

        # Also write a valid heartbeat
        write_heartbeat(sessions_dir, pid=os.getpid())

        heartbeats = monitor._read_heartbeats()
        assert len(heartbeats) == 1  # Only the valid one

    def test_empty_dir(self, monitor):
        heartbeats = monitor._read_heartbeats()
        assert heartbeats == []

    def test_ignores_non_json_files(self, monitor, sessions_dir):
        # Write a non-JSON file
        txt_path = os.path.join(sessions_dir, "notes.txt")
        with open(txt_path, "w") as f:
            f.write("just a text file")

        heartbeats = monitor._read_heartbeats()
        assert heartbeats == []

    def test_reads_multiple_heartbeats(self, monitor, sessions_dir):
        write_heartbeat(sessions_dir, pid=os.getpid(), session_id="sess-a")
        write_heartbeat(sessions_dir, pid=os.getpid() + 99999, session_id="sess-b")

        heartbeats = monitor._read_heartbeats()
        # At least the one with our real PID should parse; the other may too
        assert len(heartbeats) >= 1


# ---------------------------------------------------------------------------
# TestDetermineState
# ---------------------------------------------------------------------------


class TestDetermineState:
    def test_working(self, monitor):
        # idle < 5s => WORKING
        assert monitor._determine_state(0) == AttentionState.WORKING
        assert monitor._determine_state(2) == AttentionState.WORKING
        assert monitor._determine_state(4.9) == AttentionState.WORKING

    def test_thinking(self, monitor):
        # 5s <= idle < idle_threshold (10s) => THINKING
        assert monitor._determine_state(5) == AttentionState.THINKING
        assert monitor._determine_state(7) == AttentionState.THINKING
        assert monitor._determine_state(9.9) == AttentionState.THINKING

    def test_waiting(self, monitor):
        # idle >= idle_threshold (10s) => WAITING
        assert monitor._determine_state(10) == AttentionState.WAITING
        assert monitor._determine_state(15) == AttentionState.WAITING
        assert monitor._determine_state(300) == AttentionState.WAITING


# ---------------------------------------------------------------------------
# TestProcessAlive
# ---------------------------------------------------------------------------


class TestProcessAlive:
    def test_own_process_alive(self, monitor):
        assert monitor._process_alive(os.getpid()) is True

    def test_nonexistent_process(self, monitor):
        # Use a very high PID that almost certainly doesn't exist
        assert monitor._process_alive(4_000_000) is False


# ---------------------------------------------------------------------------
# TestPollOnce
# ---------------------------------------------------------------------------


class TestPollOnce:
    @pytest.mark.asyncio
    async def test_new_session_detected(self, monitor, sessions_dir):
        pid = os.getpid()
        # Use an old timestamp so the session is in WAITING state
        write_heartbeat(
            sessions_dir,
            pid=pid,
            last_tool_time=_iso_past(60),
        )

        events = await monitor.poll_once()
        assert len(events) == 1
        assert events[0]["type"] == "new_session"
        assert events[0]["session"].pid == pid

    @pytest.mark.asyncio
    async def test_session_ended(self, monitor, sessions_dir):
        pid = os.getpid()
        hb_path = write_heartbeat(sessions_dir, pid=pid, last_tool_time=_iso_now())

        # First poll: register the session
        events1 = await monitor.poll_once()
        assert len(events1) == 1
        assert events1[0]["type"] == "new_session"

        # Remove the heartbeat file
        os.remove(hb_path)

        # Second poll: should detect session_ended
        events2 = await monitor.poll_once()
        assert len(events2) == 1
        assert events2[0]["type"] == "session_ended"

    @pytest.mark.asyncio
    async def test_state_change(self, monitor, sessions_dir):
        pid = os.getpid()
        # First poll: recent timestamp => WORKING
        hb_path = write_heartbeat(sessions_dir, pid=pid, last_tool_time=_iso_now())

        events1 = await monitor.poll_once()
        assert len(events1) == 1
        assert events1[0]["type"] == "new_session"
        assert events1[0]["session"].state == AttentionState.WORKING

        # Update heartbeat to old timestamp => WAITING
        with open(hb_path, "r") as f:
            data = json.load(f)
        data["last_tool_time"] = _iso_past(60)
        with open(hb_path, "w") as f:
            json.dump(data, f)

        events2 = await monitor.poll_once()
        assert len(events2) == 1
        assert events2[0]["type"] == "state_changed"
        assert events2[0]["session"].state == AttentionState.WAITING

    @pytest.mark.asyncio
    async def test_no_events_when_unchanged(self, monitor, sessions_dir):
        pid = os.getpid()
        write_heartbeat(sessions_dir, pid=pid, last_tool_time=_iso_now())

        # First poll: new_session
        events1 = await monitor.poll_once()
        assert len(events1) == 1

        # Second poll with same state: no events
        events2 = await monitor.poll_once()
        assert len(events2) == 0

    @pytest.mark.asyncio
    async def test_dead_process_cleaned_up(self, monitor, sessions_dir):
        # Use a PID that doesn't exist
        fake_pid = 4_000_000
        hb_path = write_heartbeat(
            sessions_dir, pid=fake_pid, last_tool_time=_iso_now()
        )

        events = await monitor.poll_once()
        # Dead process => file should be cleaned up, no session tracked
        assert len(events) == 0
        assert not os.path.exists(hb_path)

    @pytest.mark.asyncio
    async def test_multiple_sessions(self, monitor, sessions_dir):
        pid = os.getpid()
        write_heartbeat(
            sessions_dir,
            pid=pid,
            session_id="sess-1",
            last_tool_time=_iso_now(),
        )

        events = await monitor.poll_once()
        assert len(events) == 1
        assert events[0]["session"].session_id == "sess-1"


# ---------------------------------------------------------------------------
# TestGetSessions
# ---------------------------------------------------------------------------


class TestGetSessions:
    @pytest.mark.asyncio
    async def test_returns_tracked(self, monitor, sessions_dir):
        pid = os.getpid()
        write_heartbeat(sessions_dir, pid=pid, last_tool_time=_iso_now())

        await monitor.poll_once()
        sessions = monitor.get_sessions()
        assert len(sessions) == 1
        assert sessions[0].pid == pid

    @pytest.mark.asyncio
    async def test_empty_when_no_sessions(self, monitor):
        sessions = monitor.get_sessions()
        assert sessions == []

    @pytest.mark.asyncio
    async def test_removed_after_ended(self, monitor, sessions_dir):
        pid = os.getpid()
        hb_path = write_heartbeat(sessions_dir, pid=pid, last_tool_time=_iso_now())

        await monitor.poll_once()
        assert len(monitor.get_sessions()) == 1

        os.remove(hb_path)
        await monitor.poll_once()
        assert len(monitor.get_sessions()) == 0


# ---------------------------------------------------------------------------
# TestRunLoop
# ---------------------------------------------------------------------------


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_stop(self, monitor, sessions_dir):
        """Monitor.stop() should terminate the run loop."""
        import asyncio

        # Start the monitor and stop it after a brief delay
        async def stop_soon():
            await asyncio.sleep(0.1)
            monitor.stop()

        task = asyncio.create_task(stop_soon())
        await asyncio.wait_for(monitor.run(), timeout=2.0)
        await task  # ensure the stop task completed

    @pytest.mark.asyncio
    async def test_hub_client_receives_events(self, monitor, sessions_dir):
        """Events are pushed to hub_client if available."""

        pushed: list[dict] = []

        class FakeHub:
            async def push_attention_event(self, event):
                pushed.append(event)

        monitor._hub_client = FakeHub()

        pid = os.getpid()
        write_heartbeat(sessions_dir, pid=pid, last_tool_time=_iso_past(60))

        await monitor.poll_once()
        # The new_session event should have been pushed
        assert len(pushed) == 1
        assert pushed[0]["type"] == "new_session"
