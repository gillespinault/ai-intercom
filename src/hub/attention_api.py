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

        # Forward to daemon (prefer PTY relay over tmux)
        respond_body: dict = {"keys": keys}
        if session.pty_port:
            respond_body["pty_port"] = session.pty_port
        elif session.tmux_session:
            respond_body["tmux_session"] = session.tmux_session
        else:
            return {"status": "error", "error": "Session has no pty_port or tmux_session"}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{daemon_url}/api/attention/respond",
                    json=respond_body,
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
                if session.pty_port:
                    resp = await client.get(
                        f"{daemon_url}/api/attention/terminal-pty/{session.pty_port}",
                    )
                elif session.tmux_session:
                    resp = await client.get(
                        f"{daemon_url}/api/attention/terminal/{session.tmux_session}",
                    )
                else:
                    return {"status": "error", "error": "No pty_port or tmux_session"}
                return resp.json()
        except Exception as e:
            logger.error("Failed to proxy terminal from daemon: %s", e)
            return {"status": "error", "error": str(e)}

    @router.get("/prefs")
    async def get_notification_prefs():
        """Return current Telegram notification preferences."""
        return store.get_notification_prefs()

    @router.patch("/prefs")
    async def update_notification_prefs(request: Request):
        """Update Telegram notification preferences (partial merge)."""
        updates = await request.json()
        updated = store.update_notification_prefs(updates)
        # Broadcast to all PWA clients so they stay in sync
        await store.broadcast({"type": "prefs_updated", "prefs": updated})
        return updated

    @router.post("/stats")
    async def receive_stats(request: Request):
        """Receive usage stats pushed by a daemon."""
        data = await request.json()
        stats = data.get("stats", {})
        machine_id = data.get("machine_id", "")
        store.update_usage_stats(stats)
        await store.broadcast({
            "type": "usage_stats",
            "stats": stats,
            "machine_id": machine_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return {"status": "ok"}

    @router.get("/stats")
    async def get_stats():
        """Return the latest usage stats."""
        return store.get_usage_stats()

    # ------------------------------------------------------------------
    # Permission approval endpoints
    # ------------------------------------------------------------------

    @router.post("/permission")
    async def receive_permission_request(request: Request):
        """Receive a permission request forwarded by a daemon."""
        from src.shared.models import PermissionRequest

        data = await request.json()
        session_id = data.get("session_id", "")
        project = data.get("project", "")

        # Enrich project from attention session store if daemon didn't provide it
        if not project:
            att_session = store.get_session(session_id)
            if att_session and att_session.project:
                project = att_session.project

        req = PermissionRequest(
            session_id=session_id,
            tool_name=data.get("tool_name", ""),
            tool_input=data.get("tool_input", {}),
            permission_suggestions=data.get("permission_suggestions", []),
            machine=data.get("machine", ""),
            project=project,
        )
        if data.get("request_id"):
            req.request_id = data["request_id"]

        # Dedup: cancel previous permissions for the same session
        cancelled = store.add_pending_permission(req)
        for rid in cancelled:
            await store.broadcast({
                "type": "permission_resolved",
                "request_id": rid,
                "expired": True,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        await store.broadcast({
            "type": "permission_request",
            "request": req.model_dump(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        return {"status": "pending", "request_id": req.request_id}

    @router.post("/permission/{request_id}/decide")
    async def decide_permission(request_id: str, request: Request):
        """Resolve a pending permission request with allow/deny."""
        from src.shared.models import PermissionDecision

        data = await request.json()
        perm = store.get_pending_permission(request_id)
        if not perm:
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=404, content={"error": "not_found"})

        decision = PermissionDecision(
            behavior=data.get("decision", "deny"),
            reason=data.get("reason", ""),
        )
        store.resolve_permission(request_id, decision)

        # Callback daemon to unblock the waiting hook
        machine_info = await registry.get_machine(perm.machine)
        if machine_info and machine_info.get("daemon_url"):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        f"{machine_info['daemon_url']}/api/attention/permission/resolve",
                        json={
                            "request_id": request_id,
                            "decision": decision.behavior,
                            "reason": decision.reason,
                        },
                    )
            except Exception as e:
                logger.warning("Failed to callback daemon for permission %s: %s", request_id, e)

        await store.broadcast({
            "type": "permission_resolved",
            "request_id": request_id,
            "decision": decision.behavior,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        return {"status": "resolved", "request_id": request_id, "decision": decision.behavior}

    @router.post("/permission/{request_id}/cancel")
    async def cancel_permission(request_id: str):
        """Cancel a timed-out permission request (called by daemon)."""
        perm = store.get_pending_permission(request_id)
        if not perm:
            return {"status": "not_found"}

        store._pending_permissions.pop(request_id, None)

        await store.broadcast({
            "type": "permission_resolved",
            "request_id": request_id,
            "expired": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        return {"status": "cancelled", "request_id": request_id}

    @router.get("/permission/pending")
    async def list_pending_permissions():
        """List all pending permission requests."""
        pending = store.list_pending_permissions()
        return {"pending": [p.model_dump() for p in pending]}

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
                "pending_permissions": [p.model_dump() for p in store.list_pending_permissions()],
                "usage_stats": store.get_usage_stats(),
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
                                daemon_url = machine["daemon_url"]
                                respond_body: dict = {"keys": keys}
                                if session.pty_port:
                                    respond_body["pty_port"] = session.pty_port
                                elif session.tmux_session:
                                    respond_body["tmux_session"] = session.tmux_session
                                logger.info(
                                    "WS respond: session=%s keys=%r -> %s",
                                    session_id[:12], keys, daemon_url,
                                )
                                try:
                                    async with httpx.AsyncClient(timeout=10) as client:
                                        resp = await client.post(
                                            f"{daemon_url}/api/attention/respond",
                                            json=respond_body,
                                        )
                                        logger.info("WS respond result: %s", resp.text)
                                except Exception as e:
                                    logger.warning("WebSocket respond failed: %s", e)
                            else:
                                logger.warning(
                                    "WS respond: no daemon_url for machine=%s",
                                    session.machine,
                                )
                        else:
                            logger.warning("WS respond: session %s not found in store", session_id[:12])

                elif action == "permission_decide":
                    request_id = msg.get("request_id", "")
                    decision_str = msg.get("decision", "deny")
                    reason = msg.get("reason", "")

                    from src.shared.models import PermissionDecision
                    perm = store.get_pending_permission(request_id)
                    if perm:
                        decision = PermissionDecision(behavior=decision_str, reason=reason)
                        store.resolve_permission(request_id, decision)

                        # Callback daemon to unblock hook
                        machine_data = await registry.get_machine(perm.machine)
                        if machine_data and machine_data.get("daemon_url"):
                            try:
                                async with httpx.AsyncClient(timeout=10) as http_client:
                                    await http_client.post(
                                        f"{machine_data['daemon_url']}/api/attention/permission/resolve",
                                        json={
                                            "request_id": request_id,
                                            "decision": decision_str,
                                            "reason": reason,
                                        },
                                    )
                            except Exception as e:
                                logger.warning("WS permission decide failed: %s", e)

                        await store.broadcast({
                            "type": "permission_resolved",
                            "request_id": request_id,
                            "decision": decision_str,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })

        except WebSocketDisconnect:
            pass
        finally:
            store.unsubscribe(websocket)

    # ------------------------------------------------------------------
    # Presence endpoint
    # ------------------------------------------------------------------

    @router.get("/presence")
    async def presence():
        """Return connected clients count and TTS preferences."""
        tts = store.get_tts_prefs()
        return {
            "connected_clients": len(store._subscribers),
            "active_sessions": len(store.get_all_sessions()),
            "tts": tts,
        }

    @router.get("/tts-prefs")
    async def get_tts_prefs():
        """Return current TTS preferences."""
        return store.get_tts_prefs()

    @router.patch("/tts-prefs")
    async def update_tts_prefs(request: Request):
        """Update TTS preferences (partial merge). Synced from PWA."""
        updates = await request.json()
        updated = store.update_tts_prefs(updates)
        await store.broadcast({"type": "tts_prefs_updated", "tts_prefs": updated})
        return updated

    # ------------------------------------------------------------------
    # Dispatcher preferences
    # ------------------------------------------------------------------

    @router.get("/dispatcher-prefs")
    async def get_dispatcher_prefs():
        """Return current dispatcher preferences."""
        return store.get_dispatcher_prefs()

    @router.patch("/dispatcher-prefs")
    async def update_dispatcher_prefs(request: Request):
        """Update dispatcher preferences (partial merge)."""
        updates = await request.json()
        updated = store.update_dispatcher_prefs(updates)
        await store.broadcast({"type": "dispatcher_prefs_updated", "dispatcher_prefs": updated})
        return updated

    # ------------------------------------------------------------------
    # TTS announce endpoint
    # ------------------------------------------------------------------

    @router.post("/announce")
    async def announce(request: Request):
        """Broadcast a TTS announcement to all connected PWA clients.

        The request body should contain at least a ``message`` field.
        Optional fields: ``machine_id``, ``session_id``, ``project``,
        ``category`` (default ``"milestone"``), ``priority`` (default ``"normal"``).
        """
        data = await request.json()
        message = (data.get("message") or "").strip()

        if not message:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=400,
                content={"error": "message is required"},
            )

        await store.broadcast({
            "type": "tts_announce",
            "session_id": data.get("session_id", ""),
            "project": data.get("project", ""),
            "message": message,
            "category": data.get("category", "milestone"),
            "priority": data.get("priority", "normal"),
            "machine_id": data.get("machine_id", ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        return {"status": "ok"}

    # ------------------------------------------------------------------
    # TTS proxy endpoint
    # ------------------------------------------------------------------

    import time

    _tts_last_request_time: dict[str, float] = {}

    @router.post("/tts")
    async def tts_proxy(request: Request):
        """Proxy TTS requests to the XTTS service.

        Body: {"text": "...", "language": "fr"}
        Returns: audio/raw (PCM bytes)
        """
        from fastapi.responses import Response, JSONResponse

        tts_url = getattr(request.app.state, "tts_url", "") or ""
        if not tts_url:
            return JSONResponse(
                status_code=503,
                content={"error": "TTS service not configured"},
            )

        data = await request.json()
        text = (data.get("text") or "").strip()
        language = data.get("language", "fr")

        if not text:
            return JSONResponse(
                status_code=400,
                content={"error": "text is required"},
            )

        # Rate limit: max 1 request per 2 seconds
        now = time.monotonic()
        last = _tts_last_request_time.get("last", 0.0)
        if now - last < 2.0:
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limited — max 1 request per 2 seconds"},
            )
        _tts_last_request_time["last"] = now

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # tts_url may be a base URL (http://host:port) or full endpoint
                # (http://host:port/v1/tts). Append /v1/tts only if not present.
                endpoint = tts_url if "/v1/tts" in tts_url else f"{tts_url}/v1/tts"
                resp = await client.post(
                    endpoint,
                    json={"text": text, "language": language, "sample_rate": 24000},
                )
                if resp.status_code != 200:
                    return JSONResponse(
                        status_code=502,
                        content={"error": f"XTTS returned {resp.status_code}"},
                    )
                return Response(
                    content=resp.content,
                    media_type="audio/raw",
                )
        except Exception as e:
            logger.error("TTS proxy failed: %s", e)
            return JSONResponse(
                status_code=502,
                content={"error": str(e)},
            )

    return router


def create_pwa_router() -> APIRouter:
    """Serve the PWA static files."""
    from pathlib import Path

    from fastapi.responses import FileResponse, HTMLResponse

    router = APIRouter(tags=["pwa"])
    pwa_dir = Path(__file__).parent.parent.parent / "pwa"

    @router.get("/")
    async def pwa_root_redirect():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/attention")

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
