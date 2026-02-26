"""Hub HTTP API: registration, heartbeat, join/approve, and discovery."""

from __future__ import annotations

import json
import secrets
from typing import Any

from fastapi import FastAPI, Request, Response

from src.hub.registry import Registry
from src.shared.auth import verify_request
from src.shared.config import IntercomConfig


def _normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Normalize header keys to the title-case format expected by verify_request.

    Starlette lowercases all header keys (e.g. 'x-intercom-timestamp'),
    but verify_request expects 'X-Intercom-Timestamp'.
    """
    return {key.title(): value for key, value in headers.items()}


def create_hub_api(
    registry: Registry,
    router: Any,
    config: IntercomConfig,
) -> FastAPI:
    """Create the Hub FastAPI application."""
    app = FastAPI(title="AI-Intercom Hub")
    app.state.registry = registry
    app.state.router = router
    app.state.pending_joins: dict[str, dict] = {}

    @app.get("/api/discover")
    async def discover():
        return {"hub": True, "name": "AI-Intercom Hub", "version": "0.1.0"}

    @app.post("/api/join")
    async def join(request: Request):
        data = await request.json()
        machine_id = data.get("machine_id", "")
        display_name = data.get("display_name", machine_id)

        # Store as pending, will be approved via Telegram
        app.state.pending_joins[machine_id] = {
            "machine_id": machine_id,
            "display_name": display_name,
            "projects": data.get("projects", []),
            "ip": request.client.host if request.client else "unknown",
        }

        return {"status": "pending_approval", "machine_id": machine_id}

    @app.post("/api/join/approve/{machine_id}")
    async def approve_join(machine_id: str):
        pending = app.state.pending_joins.pop(machine_id, None)
        if not pending:
            return Response(status_code=404, content="No pending join")

        token = f"ict_{machine_id}_{secrets.token_hex(16)}"
        await registry.register_machine(
            machine_id=machine_id,
            display_name=pending["display_name"],
            tailscale_ip=pending.get("ip", ""),
            daemon_url=f"http://{pending.get('ip', 'unknown')}:7700",
            token=token,
        )
        return {"status": "approved", "token": token}

    @app.post("/api/register")
    async def register(request: Request):
        body = await request.body()
        data = json.loads(body)
        machine_id = data.get("machine_id", "")
        token = await registry.get_machine_token(machine_id)

        if token:
            headers = _normalize_headers(dict(request.headers))
            if not verify_request(body, headers, token):
                return Response(status_code=401, content="Unauthorized")

        for project in data.get("projects", []):
            await registry.register_project(
                machine_id=machine_id,
                project_id=project.get("id", ""),
                description=project.get("description", ""),
                capabilities=project.get("capabilities", []),
                path=project.get("path", ""),
                agent_command=project.get("agent_command", "claude"),
            )

        return {"status": "registered", "machine_id": machine_id}

    @app.post("/api/heartbeat")
    async def heartbeat(request: Request):
        body = await request.body()
        data = json.loads(body)
        machine_id = data.get("machine_id", "")

        token = await registry.get_machine_token(machine_id)
        if token:
            headers = _normalize_headers(dict(request.headers))
            if not verify_request(body, headers, token):
                return Response(status_code=401, content="Unauthorized")

        await registry.update_heartbeat(
            machine_id, active_agents=data.get("active_agents", [])
        )
        return {"status": "ok"}

    @app.get("/api/agents")
    async def list_agents(filter: str = "all"):
        agents = await registry.list_agents(
            filter_status=filter if filter != "all" else None
        )
        return {"agents": agents}

    @app.get("/api/machines")
    async def list_machines():
        machines = await registry.list_machines()
        return {"machines": machines}

    return app
