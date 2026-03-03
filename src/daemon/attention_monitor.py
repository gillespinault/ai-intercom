"""Attention monitor sidecar loop.

Periodically reads heartbeat files from ``/tmp/cc-sessions/``, determines each
session's state (working / thinking / waiting), captures terminal output via
tmux when a session is waiting, parses prompts, and pushes state-change events
to the hub.

Usage::

    monitor = AttentionMonitor(machine_id="laptop", hub_client=hub)
    await monitor.run()   # runs until monitor.stop() is called
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import subprocess
from datetime import datetime, timezone

from src.daemon.prompt_parser import parse_notification_data, parse_terminal_output
from src.shared.models import (
    AttentionHeartbeat,
    AttentionSession,
    AttentionState,
    DetectedPrompt,
)

logger = logging.getLogger(__name__)

# Threshold (seconds) below which a session is considered actively working.
_THINKING_THRESHOLD = 5.0

# How often (seconds) to re-push a session to the hub even if state is unchanged.
# Must be well under STALE_TIMEOUT_SECONDS (300s) on the hub side.
_KEEPALIVE_INTERVAL = 120

# Sessions idle longer than this are considered abandoned and will no longer
# be pushed to the hub.  The hub's own STALE_TIMEOUT (300s) will then clean
# them up automatically.  If the user returns and types something, idle_seconds
# drops back below this threshold and the session reappears.
_ABANDON_THRESHOLD = 3600  # 1 hour


class AttentionMonitor:
    """Monitors Claude Code sessions via heartbeat files.

    Parameters
    ----------
    machine_id:
        This machine's identifier.
    sessions_dir:
        Directory where heartbeat JSON files are written (one per PID).
    hub_client:
        Optional object with an async ``push_attention_event(event)`` method.
    idle_threshold:
        Seconds of inactivity before a session is considered *waiting*.
    poll_interval:
        Seconds between poll cycles in the ``run()`` loop.
    """

    def __init__(
        self,
        machine_id: str,
        sessions_dir: str = "/tmp/cc-sessions",
        hub_client: object | None = None,
        idle_threshold: float = 15,
        poll_interval: float = 3,
    ) -> None:
        self._machine_id = machine_id
        self._sessions_dir = sessions_dir
        self._hub_client = hub_client
        self._idle_threshold = idle_threshold
        self._poll_interval = poll_interval

        self._tracked: dict[str, AttentionSession] = {}
        self._last_prompt: dict[str, DetectedPrompt] = {}
        self._last_push: dict[str, datetime] = {}  # last time an event was pushed per session
        self._running = False
        self._stop_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_heartbeats(self) -> list[AttentionHeartbeat]:
        """Read all ``*.json`` files from *sessions_dir* and parse them."""
        results: list[AttentionHeartbeat] = []
        pattern = os.path.join(self._sessions_dir, "*.json")
        for path in glob.glob(pattern):
            try:
                with open(path) as f:
                    data = json.load(f)
                results.append(AttentionHeartbeat(**data))
            except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
                logger.debug("Skipping invalid heartbeat %s: %s", path, exc)
            except OSError as exc:
                logger.debug("Cannot read heartbeat %s: %s", path, exc)
        return results

    def _determine_state(self, idle_seconds: float) -> AttentionState:
        """Map idle duration to an attention state.

        * ``< 5s`` -- WORKING
        * ``5s .. idle_threshold`` -- THINKING
        * ``>= idle_threshold`` -- WAITING
        """
        if idle_seconds < _THINKING_THRESHOLD:
            return AttentionState.WORKING
        if idle_seconds < self._idle_threshold:
            return AttentionState.THINKING
        return AttentionState.WAITING

    @staticmethod
    def _prompt_changed(prev: DetectedPrompt | None, cur: DetectedPrompt | None) -> bool:
        """Return True if the detected prompt content has meaningfully changed."""
        if prev is None and cur is None:
            return False
        if prev is None or cur is None:
            return True
        # Different prompt type is always a change.
        if prev.type != cur.type:
            return True
        # Same type — compare distinguishing fields.
        if prev.type == "permission":
            return prev.tool != cur.tool or prev.command_preview != cur.command_preview
        if prev.type == "question":
            # Compare question text, command preview, and choices.
            # Many prompts share the same question ("Do you want to proceed?")
            # but differ in what command they're asking about.
            if prev.question != cur.question:
                return True
            if prev.command_preview != cur.command_preview:
                return True
            prev_keys = [c.key for c in (prev.choices or [])]
            cur_keys = [c.key for c in (cur.choices or [])]
            return prev_keys != cur_keys
        # text_input: no distinguishing content to compare.
        return False

    @staticmethod
    def _process_alive(pid: int) -> bool:
        """Return ``True`` if a process with *pid* exists."""
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    @staticmethod
    def _capture_terminal(tmux_session: str) -> str | None:
        """Capture the last 30 lines of a tmux pane.

        Uses ``-S -30`` (start line relative to cursor) which is
        compatible with tmux 3.x. The older ``-l`` flag is not
        supported in all versions.
        """
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", tmux_session, "-p", "-S", "-30"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return result.stdout
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            logger.debug("tmux capture failed for %s: %s", tmux_session, exc)
        return None

    @staticmethod
    def _inject_response(tmux_session: str, keys: str) -> bool:
        """Send keystrokes into a tmux session.

        Handles two interaction modes:

        * **SelectInput** (keys prefixed with ``select:``): Claude Code's
          arrow-navigable menus.  The value after ``select:`` is the
          offset from the currently focused option.  ``select:0`` means
          the option is already focused -- just press Enter.  ``select:1``
          means press Down once then Enter, etc.
        * **Legacy / character keys**: Sends the literal key string
          followed by Enter (e.g. ``y``, ``n``, ``a``).
        """
        try:
            if keys.startswith("select:"):
                # SelectInput navigation: Down × offset, then Enter.
                try:
                    offset = int(keys.split(":", 1)[1])
                except (ValueError, IndexError):
                    offset = 0

                cmd = ["tmux", "send-keys", "-t", tmux_session]
                if offset > 0:
                    for _ in range(offset):
                        cmd.append("Down")
                elif offset < 0:
                    for _ in range(abs(offset)):
                        cmd.append("Up")
                cmd.append("Enter")

                logger.info(
                    "SelectInput inject: tmux=%s offset=%d cmd=%s",
                    tmux_session, offset, cmd[3:],
                )
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=5,
                )
                return result.returncode == 0

            # Legacy: send character key + Enter.
            result = subprocess.run(
                ["tmux", "send-keys", "-t", tmux_session, keys, "Enter"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            logger.debug("tmux send-keys failed for %s: %s", tmux_session, exc)
            return False

    # ------------------------------------------------------------------
    # Poll cycle
    # ------------------------------------------------------------------

    async def poll_once(self) -> list[dict]:
        """Execute one poll cycle.

        Returns a list of event dicts, each with a ``type`` key
        (``new_session``, ``state_changed``, ``session_ended``) and a
        ``session`` key holding an :class:`AttentionSession`.
        """
        events: list[dict] = []
        now = datetime.now(timezone.utc)

        heartbeats = self._read_heartbeats()
        seen_session_ids: set[str] = set()

        for hb in heartbeats:
            # Skip dead processes and clean up their heartbeat files.
            if not self._process_alive(hb.pid):
                hb_path = os.path.join(self._sessions_dir, f"{hb.pid}.json")
                try:
                    os.remove(hb_path)
                    logger.debug("Removed stale heartbeat for dead PID %d", hb.pid)
                except OSError:
                    pass
                continue

            seen_session_ids.add(hb.session_id)

            # Calculate idle time.
            idle_seconds = 0.0
            if hb.last_tool_time:
                try:
                    last_tool_dt = datetime.fromisoformat(
                        hb.last_tool_time.replace("Z", "+00:00")
                    )
                    idle_seconds = max(0.0, (now - last_tool_dt).total_seconds())
                except ValueError:
                    pass

            # Skip abandoned sessions — idle too long, likely a forgotten
            # tmux.  Stop pushing so the hub expires it after 5 min.
            if idle_seconds >= _ABANDON_THRESHOLD:
                if hb.session_id in self._tracked:
                    logger.info(
                        "Session %s (%s) abandoned (idle %.0fs), stopping updates",
                        hb.session_id[:8], hb.project, idle_seconds,
                    )
                    ended = self._tracked.pop(hb.session_id)
                    self._last_prompt.pop(hb.session_id, None)
                    self._last_push.pop(hb.session_id, None)
                    ended.state = AttentionState.ENDED
                    ended.state_since = now.isoformat()
                    events.append({"type": "session_ended", "session": ended})
                continue

            state = self._determine_state(idle_seconds)

            # If waiting, try to capture prompt details.
            # Priority: tmux terminal capture > notification_data fallback > cached prompt (B2).
            prompt = None
            if state == AttentionState.WAITING:
                if hb.tmux_session:
                    raw_output = self._capture_terminal(hb.tmux_session)
                    if raw_output:
                        prompt = parse_terminal_output(raw_output)
                if prompt is None and hb.notification_data:
                    prompt = parse_notification_data(hb.notification_data)
                # Cache the prompt for B2 race condition protection
                if prompt is not None:
                    self._last_prompt[hb.session_id] = prompt
            elif state == AttentionState.THINKING:
                # Brief idle (5-15s): keep cached prompt (B2 race protection).
                # Hook heartbeat can briefly reset idle_seconds even though
                # the terminal still shows a prompt.
                prompt = self._last_prompt.get(hb.session_id)
            else:
                # WORKING: agent is actively making tool calls — old prompt
                # is stale.  Clear the cache so the UI doesn't show
                # phantom yes/no buttons.
                prompt = None
                self._last_prompt.pop(hb.session_id, None)

            session = AttentionSession(
                session_id=hb.session_id,
                machine=hb.machine,
                project=hb.project,
                session_name=hb.session_name,
                pid=hb.pid,
                state=state,
                state_since=now.isoformat(),
                last_tool=hb.last_tool,
                last_tool_time=hb.last_tool_time,
                rc_url=hb.rc_url,
                idle_seconds=int(idle_seconds),
                prompt=prompt,
                tmux_session=hb.tmux_session,
            )

            # Compare with tracked state.
            prev = self._tracked.get(hb.session_id)
            if prev is None:
                events.append({"type": "new_session", "session": session})
                self._last_push[hb.session_id] = now
            elif prev.state != session.state:
                events.append({"type": "state_changed", "session": session})
                self._last_push[hb.session_id] = now
            elif self._prompt_changed(prev.prompt, session.prompt):
                # Prompt content changed within the same state (e.g. new
                # permission prompt while still WAITING).  Push an update
                # so the hub and PWA reflect the fresh prompt.
                session.state_since = prev.state_since
                events.append({"type": "state_changed", "session": session})
                self._last_push[hb.session_id] = now
            else:
                # State unchanged -- keep the original state_since timestamp.
                session.state_since = prev.state_since
                # Send periodic keepalive so the hub doesn't expire this session.
                last_push = self._last_push.get(hb.session_id, now)
                if (now - last_push).total_seconds() >= _KEEPALIVE_INTERVAL:
                    events.append({"type": "keepalive", "session": session})
                    self._last_push[hb.session_id] = now

            self._tracked[hb.session_id] = session

        # Detect ended sessions (tracked but no longer present).
        ended_ids = set(self._tracked.keys()) - seen_session_ids
        for sid in ended_ids:
            ended_session = self._tracked.pop(sid)
            self._last_prompt.pop(sid, None)  # B2: clean prompt cache
            self._last_push.pop(sid, None)
            ended_session.state = AttentionState.ENDED
            ended_session.state_since = now.isoformat()
            events.append({"type": "session_ended", "session": ended_session})

        # Push events to hub if a client is configured.
        if self._hub_client is not None:
            for event in events:
                try:
                    await self._hub_client.push_attention_event(event)  # type: ignore[union-attr]
                except Exception as exc:
                    logger.warning("Failed to push event to hub: %s", exc)

        return events

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Poll continuously until :meth:`stop` is called."""
        self._running = True
        self._stop_event.clear()
        logger.info(
            "Attention monitor started (dir=%s, interval=%.1fs, threshold=%.1fs)",
            self._sessions_dir,
            self._poll_interval,
            self._idle_threshold,
        )
        while self._running:
            try:
                await self.poll_once()
            except Exception as exc:
                logger.error("Poll error: %s", exc, exc_info=True)
            # Wait for poll_interval OR until stop() is called.
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval
                )
                # If we get here, stop_event was set.
                break
            except asyncio.TimeoutError:
                # Normal: poll_interval elapsed, loop again.
                pass

    def stop(self) -> None:
        """Signal the ``run()`` loop to exit."""
        self._running = False
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    def get_sessions(self) -> list[AttentionSession]:
        """Return a snapshot of all currently tracked sessions."""
        return list(self._tracked.values())

    async def resync(self) -> int:
        """Re-push all tracked sessions as ``new_session`` events.

        Called when the hub restarts (detected via epoch change) so that
        the hub's in-memory AttentionStore is repopulated.

        Returns the number of sessions pushed.
        """
        if not self._hub_client or not self._tracked:
            return 0

        pushed = 0
        for session in list(self._tracked.values()):
            try:
                await self._hub_client.push_attention_event(  # type: ignore[union-attr]
                    {"type": "new_session", "session": session}
                )
                pushed += 1
            except Exception as exc:
                logger.warning("Resync push failed for %s: %s", session.session_id, exc)
        return pushed
