"""Hub HTTP API: registration, heartbeat, join/approve, routing, and discovery."""

from __future__ import annotations

import json
import secrets
import uuid
from typing import Any

from fastapi import FastAPI, Request, Response

from src.hub.registry import Registry
from src.shared.auth import normalize_headers, verify_request
from src.shared.config import IntercomConfig
from src.shared.models import Message


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
    app.state.mission_store: dict[str, list[dict]] = {}

    # --- Auth helper ---

    async def _verify_machine(request: Request, body: bytes, machine_id: str) -> bool:
        """Verify HMAC authentication for a known machine."""
        token = await registry.get_machine_token(machine_id)
        if not token:
            return True  # Unknown machine, no token to check
        headers = normalize_headers(dict(request.headers))
        return verify_request(body, headers, token)

    # --- Discovery ---

    @app.get("/api/discover")
    async def discover():
        return {"hub": True, "name": "AI-Intercom Hub", "version": "0.1.0"}

    # --- Join flow ---

    @app.post("/api/join")
    async def join(request: Request):
        data = await request.json()
        machine_id = data.get("machine_id", "")
        display_name = data.get("display_name", machine_id)

        app.state.pending_joins[machine_id] = {
            "machine_id": machine_id,
            "display_name": display_name,
            "projects": data.get("projects", []),
            "ip": request.client.host if request.client else "unknown",
            "status": "pending_approval",
        }

        return {"status": "pending_approval", "machine_id": machine_id}

    @app.get("/api/join/status/{machine_id}")
    async def join_status(machine_id: str):
        """Check join request status (used by install.sh polling)."""
        pending = app.state.pending_joins.get(machine_id)
        if pending:
            return {"status": pending.get("status", "pending_approval")}
        # Check if already registered
        machine = await registry.get_machine(machine_id)
        if machine:
            return {"status": "approved", "token": machine["token"]}
        return Response(status_code=404, content="No join request found")

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

    # --- Registration ---

    @app.post("/api/register")
    async def register(request: Request):
        body = await request.body()
        data = json.loads(body)
        machine_id = data.get("machine_id", "")

        if not await _verify_machine(request, body, machine_id):
            return Response(status_code=401, content="Unauthorized")

        # Ensure machine exists before registering projects
        display_name = data.get("display_name", machine_id)
        ip = request.client.host if request.client else ""
        existing = await registry.get_machine(machine_id)
        if not existing:
            await registry.register_machine(
                machine_id=machine_id,
                display_name=display_name,
                tailscale_ip=ip,
                daemon_url=f"http://{ip}:7700",
                token="",
            )

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

    @app.post("/api/register/update")
    async def register_update(request: Request):
        """Update registration for a specific machine/project."""
        body = await request.body()
        data = json.loads(body)
        machine_id = data.get("machine_id", "")

        if not await _verify_machine(request, body, machine_id):
            return Response(status_code=401, content="Unauthorized")

        action = data.get("action", "update")
        project_id = data.get("project_id", "")

        if action == "update" and data.get("machine"):
            machine_data = data["machine"]
            await registry.register_machine(
                machine_id=machine_id,
                display_name=machine_data.get("display_name", machine_id),
                tailscale_ip=machine_data.get("tailscale_ip", ""),
                daemon_url=machine_data.get("daemon_url", ""),
                token=machine_data.get("token", ""),
            )

        if action == "update" and data.get("project"):
            proj = data["project"]
            await registry.register_project(
                machine_id=machine_id,
                project_id=project_id or proj.get("id", ""),
                description=proj.get("description", ""),
                capabilities=proj.get("capabilities", []),
                path=proj.get("path", ""),
                agent_command=proj.get("agent_command", "claude"),
            )
        elif action == "remove" and project_id:
            await registry.remove_project(machine_id, project_id)

        return {"status": "updated", "machine_id": machine_id}

    # --- Heartbeat ---

    @app.post("/api/heartbeat")
    async def heartbeat(request: Request):
        body = await request.body()
        data = json.loads(body)
        machine_id = data.get("machine_id", "")

        if not await _verify_machine(request, body, machine_id):
            return Response(status_code=401, content="Unauthorized")

        await registry.update_heartbeat(
            machine_id, active_agents=data.get("active_agents", [])
        )
        return {"status": "ok"}

    # --- Message routing ---

    @app.post("/api/route")
    async def route_message(request: Request):
        """Route a message between agents via the hub router."""
        body = await request.body()
        data = json.loads(body)

        from_agent = data.get("from_agent", "")
        machine_id = from_agent.split("/")[0] if "/" in from_agent else ""
        if machine_id and not await _verify_machine(request, body, machine_id):
            return Response(status_code=401, content="Unauthorized")

        mission_id = data.get("mission_id") or str(uuid.uuid4())
        msg = Message(
            id=str(uuid.uuid4()),
            from_agent=from_agent,
            to_agent=data.get("to_agent", ""),
            type=data.get("type", "send"),
            payload=data.get("payload", {}),
            mission_id=mission_id,
        )

        # Store in mission history
        if mission_id not in app.state.mission_store:
            app.state.mission_store[mission_id] = []
        app.state.mission_store[mission_id].append(msg.model_dump())

        result = await app.state.router.route(msg)
        return result

    # --- Mission queries ---

    @app.get("/api/missions/{mission_id}")
    async def get_mission(mission_id: str):
        """Get mission status and message count."""
        history = app.state.mission_store.get(mission_id)
        if history is None:
            return Response(status_code=404, content="Mission not found")
        return {
            "mission_id": mission_id,
            "message_count": len(history),
            "last_message": history[-1] if history else None,
        }

    @app.get("/api/missions/{mission_id}/history")
    async def get_mission_history(mission_id: str, limit: int = 50):
        """Get message history for a mission."""
        history = app.state.mission_store.get(mission_id)
        if history is None:
            return Response(status_code=404, content="Mission not found")
        return {"mission_id": mission_id, "messages": history[-limit:]}

    # --- Agent/machine listing ---

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
