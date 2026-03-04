"""In-memory attention store aggregating sessions from all daemons.

Tracks :class:`AttentionSession` objects received via daemon events and
supports WebSocket broadcasting to PWA subscribers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import WebSocket

from src.shared.models import AttentionEvent, AttentionSession, AttentionState

logger = logging.getLogger(__name__)

STALE_TIMEOUT_SECONDS = 300  # 5 minutes without update = stale


class AttentionStore:
    """Aggregates attention sessions from all connected daemons.

    Sessions are keyed by ``session_id`` and updated via :meth:`handle_event`.
    WebSocket subscribers receive real-time broadcasts of all events.
    """

    _DEFAULT_PREFS: dict[str, bool] = {
        "permission": True,
        "question": True,
        "text_input": True,
    }

    def __init__(self, prefs_path: str = "data/notification_prefs.json") -> None:
        self._sessions: dict[str, AttentionSession] = {}
        self._subscribers: list[WebSocket] = []
        self._notified_waiting: set[str] = set()
        self._on_waiting_callback = None  # async callable(AttentionSession)
        self._cleanup_task: asyncio.Task | None = None
        self._prefs_path = prefs_path
        self._notification_prefs: dict[str, bool] = dict(self._DEFAULT_PREFS)
        self._load_notification_prefs()

    def set_on_waiting_callback(self, callback) -> None:
        """Set an async callback to invoke when a session enters WAITING state."""
        self._on_waiting_callback = callback

    # ------------------------------------------------------------------
    # Notification preferences
    # ------------------------------------------------------------------

    def _load_notification_prefs(self) -> None:
        """Load notification preferences from the JSON file if it exists."""
        path = Path(self._prefs_path)
        if path.is_file():
            try:
                with open(path) as f:
                    data = json.load(f)
                # Only merge known keys
                for key in self._DEFAULT_PREFS:
                    if key in data:
                        self._notification_prefs[key] = bool(data[key])
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load notification prefs from %s: %s", self._prefs_path, e)

    def _save_notification_prefs(self) -> None:
        """Persist notification preferences to the JSON file."""
        path = Path(self._prefs_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(self._notification_prefs, f, indent=2)
        except OSError as e:
            logger.warning("Failed to save notification prefs to %s: %s", self._prefs_path, e)

    def get_notification_prefs(self) -> dict[str, bool]:
        """Return a copy of the current notification preferences."""
        return dict(self._notification_prefs)

    def update_notification_prefs(self, updates: dict) -> dict[str, bool]:
        """Merge known keys from *updates* into preferences and persist.

        Unknown keys are silently ignored. Returns the updated preferences.
        """
        for key in self._DEFAULT_PREFS:
            if key in updates:
                self._notification_prefs[key] = bool(updates[key])
        self._save_notification_prefs()
        return self.get_notification_prefs()

    def should_notify_telegram(self, prompt_type: str) -> bool:
        """Return whether Telegram notifications are enabled for *prompt_type*."""
        return self._notification_prefs.get(prompt_type, True)

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def handle_event(self, machine_id: str, event: dict) -> None:
        """Process an attention event from a daemon.

        Parameters
        ----------
        machine_id:
            The machine that sent the event.
        event:
            Dict with ``type`` (``new_session``, ``state_changed``, or
            ``session_ended``) and ``session`` (dict or AttentionSession).
        """
        event_type = event.get("type", "")
        session_data = event.get("session")

        if session_data is None:
            logger.warning("Attention event from %s has no session data", machine_id)
            return

        # Normalise to AttentionSession
        if isinstance(session_data, dict):
            session = AttentionSession(**session_data)
        else:
            session = session_data

        if event_type in ("new_session", "state_changed"):
            session.last_update = datetime.now(timezone.utc).isoformat()
            self._sessions[session.session_id] = session
            # Notify on WAITING transition (debounced per session)
            if session.state == AttentionState.WAITING:
                if session.session_id not in self._notified_waiting:
                    self._notified_waiting.add(session.session_id)
                    if self._on_waiting_callback:
                        # Check notification prefs before Telegram callback
                        prompt_type = session.prompt.type if session.prompt else None
                        if prompt_type is None or self.should_notify_telegram(prompt_type):
                            import asyncio
                            asyncio.create_task(self._on_waiting_callback(session))
            elif session.session_id in self._notified_waiting:
                # Reset debounce when leaving WAITING
                self._notified_waiting.discard(session.session_id)
        elif event_type == "keepalive":
            # Refresh last_update to prevent stale cleanup, no notification.
            session.last_update = datetime.now(timezone.utc).isoformat()
            self._sessions[session.session_id] = session
        elif event_type == "session_ended":
            self._sessions.pop(session.session_id, None)
            self._notified_waiting.discard(session.session_id)
        else:
            logger.warning("Unknown attention event type %r from %s", event_type, machine_id)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_all_sessions(self) -> list[AttentionSession]:
        """Return a list of all tracked sessions."""
        return list(self._sessions.values())

    def get_waiting_sessions(self) -> list[AttentionSession]:
        """Return only sessions in the WAITING state."""
        return [s for s in self._sessions.values() if s.state == AttentionState.WAITING]

    def get_session(self, session_id: str) -> AttentionSession | None:
        """Look up a single session by its ID."""
        return self._sessions.get(session_id)

    # ------------------------------------------------------------------
    # WebSocket subscription
    # ------------------------------------------------------------------

    def subscribe(self, ws: WebSocket) -> None:
        """Register a WebSocket connection for event broadcasts."""
        self._subscribers.append(ws)

    def unsubscribe(self, ws: WebSocket) -> None:
        """Remove a WebSocket connection from the subscriber list."""
        try:
            self._subscribers.remove(ws)
        except ValueError:
            pass

    async def broadcast(self, event: dict) -> None:
        """Send *event* to all WebSocket subscribers.

        Dead connections are silently removed from the subscriber list.
        """
        dead: list[WebSocket] = []
        payload = json.dumps(event)

        for ws in self._subscribers:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)

        for ws in dead:
            try:
                self._subscribers.remove(ws)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Stale session cleanup
    # ------------------------------------------------------------------

    def start_cleanup(self) -> None:
        """Start the periodic stale session cleanup task."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        """Periodically remove sessions not updated for >5 minutes."""
        while True:
            await asyncio.sleep(60)  # Check every minute
            try:
                await self._cleanup_stale_sessions()
            except Exception as e:
                logger.error("Stale session cleanup error: %s", e)

    async def _cleanup_stale_sessions(self) -> None:
        """Remove sessions whose last_update is older than STALE_TIMEOUT_SECONDS."""
        now = datetime.now(timezone.utc)
        stale_ids: list[str] = []

        for sid, session in self._sessions.items():
            if session.last_update:
                try:
                    last = datetime.fromisoformat(session.last_update)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    age = (now - last).total_seconds()
                    if age > STALE_TIMEOUT_SECONDS:
                        stale_ids.append(sid)
                except (ValueError, TypeError):
                    pass

        for sid in stale_ids:
            session = self._sessions.pop(sid, None)
            self._notified_waiting.discard(sid)
            if session:
                logger.info(
                    "Cleaned up stale session %s (%s/%s)",
                    sid, session.machine, session.project,
                )
                await self.broadcast({
                    "type": "session_ended",
                    "session": session.model_dump() if hasattr(session, "model_dump") else {},
                })
