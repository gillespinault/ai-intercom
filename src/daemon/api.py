"""Daemon HTTP API: receives messages from the hub and exposes health/status."""

from __future__ import annotations

import json

from fastapi import FastAPI, Request, Response

from src.shared.auth import verify_request


def _normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Normalize header keys to the title-case format expected by verify_request.

    Starlette lowercases all header keys (e.g. 'x-intercom-timestamp'),
    but verify_request expects 'X-Intercom-Timestamp'.
    """
    return {key.title(): value for key, value in headers.items()}


def create_app(machine_id: str, token: str) -> FastAPI:
    """Create the daemon FastAPI application."""
    app = FastAPI(title=f"AI-Intercom Daemon ({machine_id})")
    app.state.machine_id = machine_id
    app.state.token = token
    app.state.active_missions: dict[str, dict] = {}

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
        headers = _normalize_headers(dict(request.headers))
        if not verify_request(body, headers, token):
            return Response(status_code=401, content="Unauthorized")

        data = json.loads(body)
        # Store mission reference; actual agent launch is handled by agent_launcher
        mission_id = data.get("mission_id", "unknown")
        app.state.active_missions[mission_id] = data

        return {"status": "received", "mission_id": mission_id}

    return app
