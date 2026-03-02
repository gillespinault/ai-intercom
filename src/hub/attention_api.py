"""Hub Attention API: REST endpoints + WebSocket for the PWA dashboard.

Provides endpoints for daemons to push attention events, for the PWA to
list/query sessions, and a WebSocket for real-time updates.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from src.hub.attention_store import AttentionStore
from src.hub.registry import Registry

logger = logging.getLogger(__name__)


def create_attention_router(store: AttentionStore, registry: Registry) -> APIRouter:
    """Create the attention APIRouter with all endpoints.

    Parameters
    ----------
    store:
        The shared :class:`AttentionStore` instance.
    registry:
        The hub :class:`Registry` for looking up daemon URLs.
    """
    router = APIRouter(prefix="/api/attention", tags=["attention"])

    @router.post("/event")
    async def receive_event(request: Request):
        """Receive an attention event pushed by a daemon.

        The request body should contain ``machine_id`` and ``event``
        (with ``type`` and ``session`` keys).
        """
        data = await request.json()
        machine_id = data.get("machine_id", "")
        event = data.get("event", data)

        store.handle_event(machine_id, event)

        # Broadcast to WebSocket subscribers
        broadcast_payload = {
            "type": event.get("type", "unknown"),
            "machine_id": machine_id,
            "session": event.get("session"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await store.broadcast(broadcast_payload)

        return {"status": "ok"}

    @router.get("/sessions")
    async def list_sessions():
        """Return all tracked attention sessions."""
        sessions = store.get_all_sessions()
        return {"sessions": [s.model_dump() for s in sessions]}

    @router.get("/sessions/waiting")
    async def list_waiting():
        """Return only sessions in the WAITING state."""
        sessions = store.get_waiting_sessions()
        return {"sessions": [s.model_dump() for s in sessions]}

    @router.post("/respond")
    async def respond_to_session(request: Request):
        """Forward a user response to the daemon hosting the session.

        The request body should contain ``session_id`` and ``keys``
        (the keystrokes to inject via tmux).
        """
        data = await request.json()
        session_id = data.get("session_id", "")
        keys = data.get("keys", "")

        if not session_id or not keys:
            return {"status": "error", "error": "session_id and keys are required"}

        session = store.get_session(session_id)
        if not session:
            return {"status": "error", "error": f"Session {session_id} not found"}

        # Look up the daemon URL for this machine
        machine = await registry.get_machine(session.machine)
        if not machine:
            return {"status": "error", "error": f"Machine {session.machine} not found in registry"}

        daemon_url = machine.get("daemon_url", "")
        if not daemon_url:
            return {"status": "error", "error": f"No daemon_url for machine {session.machine}"}

        # Forward to daemon
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{daemon_url}/api/attention/respond",
                    json={
                        "tmux_session": session.tmux_session,
                        "keys": keys,
                    },
                )
                result = resp.json()
                return result
        except Exception as e:
            logger.error("Failed to forward respond to daemon: %s", e)
            return {"status": "error", "error": str(e)}

    @router.get("/terminal/{session_id}")
    async def get_terminal(session_id: str):
        """Proxy terminal capture from the daemon hosting the session."""
        session = store.get_session(session_id)
        if not session:
            return {"status": "error", "error": f"Session {session_id} not found"}

        machine = await registry.get_machine(session.machine)
        if not machine:
            return {"status": "error", "error": f"Machine {session.machine} not found"}

        daemon_url = machine.get("daemon_url", "")
        if not daemon_url:
            return {"status": "error", "error": f"No daemon_url for machine {session.machine}"}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{daemon_url}/api/attention/terminal/{session.tmux_session}",
                )
                return resp.json()
        except Exception as e:
            logger.error("Failed to proxy terminal from daemon: %s", e)
            return {"status": "error", "error": str(e)}

    @router.websocket("/ws")
    async def attention_websocket(websocket: WebSocket):
        """WebSocket endpoint for real-time attention updates.

        On connect:
        - Sends an initial snapshot of all sessions.

        On receive:
        - Accepts ``respond`` commands from the PWA (``{"action": "respond",
          "session_id": "...", "keys": "..."}``).

        On disconnect:
        - Unsubscribes from the store.
        """
        await websocket.accept()
        store.subscribe(websocket)

        try:
            # Send initial snapshot
            sessions = store.get_all_sessions()
            snapshot = {
                "type": "snapshot",
                "sessions": [s.model_dump() for s in sessions],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            await websocket.send_text(json.dumps(snapshot))

            # Listen for messages from the PWA
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                action = msg.get("action", "")
                if action == "respond":
                    session_id = msg.get("session_id", "")
                    keys = msg.get("keys", "")
                    if session_id and keys:
                        session = store.get_session(session_id)
                        if session:
                            machine = await registry.get_machine(session.machine)
                            if machine and machine.get("daemon_url"):
                                try:
                                    async with httpx.AsyncClient(timeout=10) as client:
                                        await client.post(
                                            f"{machine['daemon_url']}/api/attention/respond",
                                            json={
                                                "tmux_session": session.tmux_session,
                                                "keys": keys,
                                            },
                                        )
                                except Exception as e:
                                    logger.warning("WebSocket respond failed: %s", e)

        except WebSocketDisconnect:
            pass
        finally:
            store.unsubscribe(websocket)

    return router


def create_pwa_router() -> APIRouter:
    """Serve the PWA static files."""
    from pathlib import Path

    from fastapi.responses import FileResponse, HTMLResponse

    router = APIRouter(tags=["pwa"])
    pwa_dir = Path(__file__).parent.parent.parent / "pwa"

    @router.get("/attention")
    async def pwa_index():
        index = pwa_dir / "index.html"
        if index.exists():
            return FileResponse(index, media_type="text/html")
        return HTMLResponse("<h1>Attention Hub PWA not built yet</h1>")

    @router.get("/attention/{path:path}")
    async def pwa_static(path: str):
        file = pwa_dir / path
        if file.exists() and file.is_file():
            media_types = {
                ".css": "text/css",
                ".js": "application/javascript",
                ".json": "application/json",
                ".png": "image/png",
                ".svg": "image/svg+xml",
            }
            mt = media_types.get(file.suffix, "application/octet-stream")
            return FileResponse(file, media_type=mt)
        return HTMLResponse("Not found", status_code=404)

    return router
