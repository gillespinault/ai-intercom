"""Daemon HTTP API: receives messages from the hub and exposes health/status."""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request, Response

from src.shared.auth import normalize_headers, verify_request

logger = logging.getLogger(__name__)


def create_app(machine_id: str, token: str) -> FastAPI:
    """Create the daemon FastAPI application."""
    app = FastAPI(title=f"AI-Intercom Daemon ({machine_id})")
    app.state.machine_id = machine_id
    app.state.token = token
    app.state.active_missions: dict[str, dict] = {}
    app.state.launcher = None  # Set by daemon main

    @app.get("/health")
    async def health():
        return {
            "machine_id": machine_id,
            "status": "ok",
            "active_missions": len(app.state.active_missions),
        }

    @app.get("/api/discover")
    async def discover():
        return {
            "hub": False,
            "machine_id": machine_id,
            "version": "0.1.0",
        }

    @app.get("/api/status")
    async def status():
        return {
            "machine_id": machine_id,
            "active_missions": list(app.state.active_missions.keys()),
        }

    @app.post("/api/message")
    async def receive_message(request: Request):
        body = await request.body()
        headers = normalize_headers(dict(request.headers))
        if not verify_request(body, headers, token):
            return Response(status_code=401, content="Unauthorized")

        data = json.loads(body)
        mission_id = data.get("mission_id", "unknown")
        msg_type = data.get("type", "send")
        app.state.active_missions[mission_id] = data

        # Launch agent for actionable message types
        if msg_type in ("ask", "start_agent") and app.state.launcher:
            to_agent = data.get("to_agent", "")
            project = to_agent.split("/", 1)[1] if "/" in to_agent else to_agent
            mission = data.get("payload", {}).get("mission", "")
            message = data.get("payload", {}).get("message", mission)
            try:
                result = await app.state.launcher.launch(
                    project_id=project,
                    mission=message,
                    mission_id=mission_id,
                )
                return {"status": "launched", "mission_id": mission_id, **result}
            except Exception as e:
                logger.error("Failed to launch agent: %s", e)
                return {"status": "launch_failed", "mission_id": mission_id, "error": str(e)}

        return {"status": "received", "mission_id": mission_id}

    return app
