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

        # Launch agent for actionable message types (non-blocking)
        if msg_type in ("ask", "start_agent") and app.state.launcher:
            to_agent = data.get("to_agent", "")
            project = to_agent.split("/", 1)[1] if "/" in to_agent else to_agent
            payload = data.get("payload", {})
            mission = payload.get("mission") or payload.get("message", "")
            agent_command = payload.get("agent_command")

            # Resolve project path from registry or use working directory
            project_path = "."
            if hasattr(app.state, "project_paths"):
                project_path = app.state.project_paths.get(project, ".")

            try:
                await app.state.launcher.launch_background(
                    mission=mission,
                    context_messages=[],
                    mission_id=mission_id,
                    project_path=project_path,
                    agent_command=agent_command,
                )
                return {"status": "launched", "mission_id": mission_id}
            except Exception as e:
                logger.error("Failed to launch agent: %s", e)
                return {"status": "launch_failed", "mission_id": mission_id, "error": str(e)}

        return {"status": "received", "mission_id": mission_id}

    @app.get("/api/missions/{mission_id}")
    async def mission_status(mission_id: str, feedback_since: int = 0):
        """Get the status of a mission running on this daemon."""
        if not app.state.launcher:
            return Response(status_code=404, content="No launcher configured")
        result = app.state.launcher.get_status(mission_id)
        if not result:
            return Response(status_code=404, content="Mission not found")
        return {
            "mission_id": mission_id,
            "status": result.status,
            "output": result.output,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "feedback": [
                {"timestamp": f.timestamp, "kind": f.kind, "summary": f.summary}
                for f in result.feedback[feedback_since:]
            ],
            "feedback_total": len(result.feedback),
            "turn_count": result.turn_count,
        }

    return app
