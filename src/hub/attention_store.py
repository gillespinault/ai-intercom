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

from src.shared.models import AttentionEvent, AttentionSession, AttentionState, PermissionRequest, PermissionDecision

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

    _DEFAULT_TTS_PREFS: dict = {
        "enabled": True,
        "categories": {
            "milestone": True,
            "difficulty": True,
            "didactic": True,
            "attention": True,
            "permission": True,
            "lifecycle": True,
        },
    }

    def __init__(self, prefs_path: str = "data/notification_prefs.json") -> None:
        self._sessions: dict[str, AttentionSession] = {}
        self._subscribers: list[WebSocket] = []
        self._notified_waiting: set[str] = set()
        self._on_waiting_callback = None  # async callable(AttentionSession)
        self._cleanup_task: asyncio.Task | None = None
        self._prefs_path = prefs_path
        self._notification_prefs: dict[str, bool] = dict(self._DEFAULT_PREFS)
        self._tts_prefs: dict = json.loads(json.dumps(self._DEFAULT_TTS_PREFS))
        self._usage_stats: dict = {}
        self._pending_permissions: dict[str, PermissionRequest] = {}
        self._on_permission_resolved = None
        self._load_notification_prefs()
        self._load_tts_prefs()

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

    # ------------------------------------------------------------------
    # TTS preferences
    # ------------------------------------------------------------------

    def _load_tts_prefs(self) -> None:
        path = Path(self._prefs_path).parent / "tts_prefs.json"
        if path.is_file():
            try:
                with open(path) as f:
                    data = json.load(f)
                if "enabled" in data:
                    self._tts_prefs["enabled"] = bool(data["enabled"])
                if "categories" in data and isinstance(data["categories"], dict):
                    for key in self._DEFAULT_TTS_PREFS["categories"]:
                        if key in data["categories"]:
                            self._tts_prefs["categories"][key] = bool(data["categories"][key])
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load TTS prefs: %s", e)

    def _save_tts_prefs(self) -> None:
        path = Path(self._prefs_path).parent / "tts_prefs.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w") as f:
                json.dump(self._tts_prefs, f, indent=2)
        except OSError as e:
            logger.warning("Failed to save TTS prefs: %s", e)

    def get_tts_prefs(self) -> dict:
        return json.loads(json.dumps(self._tts_prefs))

    def update_tts_prefs(self, updates: dict) -> dict:
        if "enabled" in updates:
            self._tts_prefs["enabled"] = bool(updates["enabled"])
        if "categories" in updates and isinstance(updates["categories"], dict):
            for key in self._DEFAULT_TTS_PREFS["categories"]:
                if key in updates["categories"]:
                    self._tts_prefs["categories"][key] = bool(updates["categories"][key])
        self._save_tts_prefs()
        return self.get_tts_prefs()

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
            prev_session = self._sessions.get(session.session_id)
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
            else:
                # Left WAITING — cancel any stale permission tiles
                was_waiting = (
                    session.session_id in self._notified_waiting
                    or (prev_session and prev_session.state == AttentionState.WAITING)
                )
                if was_waiting:
                    cancelled = self.cancel_permissions_for_session(session.session_id)
                    if cancelled:
                        import asyncio
                        for rid in cancelled:
                            asyncio.create_task(self.broadcast({
                                "type": "permission_resolved",
                                "request_id": rid,
                                "expired": True,
                            }))
                            logger.info(
                                "Auto-cancelled permission %s (session %s left WAITING)",
                                rid, session.session_id[:12],
                            )
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
    # Usage stats
    # ------------------------------------------------------------------

    def update_usage_stats(self, stats: dict) -> None:
        """Store the latest usage stats from a daemon."""
        self._usage_stats = stats

    def get_usage_stats(self) -> dict:
        """Return the latest usage stats."""
        return self._usage_stats

    # ------------------------------------------------------------------
    # Permission approval
    # ------------------------------------------------------------------

    def set_on_permission_resolved(self, callback) -> None:
        """Set a callback invoked when a permission is resolved."""
        self._on_permission_resolved = callback

    def add_pending_permission(self, request: PermissionRequest) -> list[str]:
        """Store a pending permission request.

        If another permission is already pending for the same ``session_id``,
        it is automatically cancelled (a session can only have one pending
        permission at a time — a new request proves the old one was resolved
        locally).

        Returns a list of cancelled ``request_id`` values (for broadcasting).
        """
        cancelled: list[str] = []
        for rid, existing in list(self._pending_permissions.items()):
            if existing.session_id == request.session_id:
                self._pending_permissions.pop(rid, None)
                cancelled.append(rid)
        self._pending_permissions[request.request_id] = request
        return cancelled

    def get_pending_permission(self, request_id: str) -> PermissionRequest | None:
        """Look up a pending permission by request_id."""
        return self._pending_permissions.get(request_id)

    def list_pending_permissions(self) -> list[PermissionRequest]:
        """Return all pending permission requests."""
        return list(self._pending_permissions.values())

    def resolve_permission(self, request_id: str, decision: PermissionDecision) -> bool:
        """Resolve a pending permission and notify listeners."""
        req = self._pending_permissions.pop(request_id, None)
        if req is None:
            return False
        if self._on_permission_resolved:
            self._on_permission_resolved(request_id, decision)
        return True

    def cancel_permissions_for_session(self, session_id: str) -> list[str]:
        """Remove all pending permissions for a given session.

        Called when a session transitions away from WAITING — proving any
        pending permission was resolved locally.

        Returns cancelled ``request_id`` values.
        """
        cancelled: list[str] = []
        for rid, req in list(self._pending_permissions.items()):
            if req.session_id == session_id:
                self._pending_permissions.pop(rid, None)
                cancelled.append(rid)
        return cancelled

    def expire_permissions(self, max_age_seconds: int = 150) -> list[str]:
        """Remove permissions older than max_age_seconds. Returns expired IDs."""
        now = datetime.now(timezone.utc)
        expired: list[str] = []
        for rid, req in list(self._pending_permissions.items()):
            try:
                created = datetime.fromisoformat(req.created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if (now - created).total_seconds() > max_age_seconds:
                    expired.append(rid)
                    self._pending_permissions.pop(rid, None)
            except (ValueError, TypeError):
                pass
        return expired

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
                # Also expire stale permission requests
                expired = self.expire_permissions()
                for rid in expired:
                    await self.broadcast({
                        "type": "permission_resolved",
                        "request_id": rid,
                        "expired": True,
                    })
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
