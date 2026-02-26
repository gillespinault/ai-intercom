from __future__ import annotations

import json
from typing import Any

import httpx

from src.shared.auth import sign_request


class HubClient:
    def __init__(self, hub_url: str, token: str, machine_id: str):
        self.hub_url = hub_url
        self.token = token
        self.machine_id = machine_id

    def _auth_headers(self, body: bytes) -> dict[str, str]:
        headers = sign_request(body, self.machine_id, self.token)
        headers["Content-Type"] = "application/json"
        return headers

    async def _post(self, path: str, data: dict) -> dict:
        body = json.dumps(data).encode()
        headers = self._auth_headers(body)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.hub_url}{path}", content=body, headers=headers
            )
            return resp.json()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{self.hub_url}{path}", params=params)
            return resp.json()

    async def list_agents(self, filter: str = "all") -> list[dict]:
        result = await self._get("/api/agents", {"filter": filter})
        return result.get("agents", [])

    async def send_message(
        self,
        from_agent: str,
        to: str,
        message: str,
        priority: str = "normal",
    ) -> dict:
        return await self._post(
            "/api/route",
            {
                "from_agent": from_agent,
                "to_agent": to,
                "type": "send",
                "payload": {"message": message, "priority": priority},
            },
        )

    async def ask(
        self,
        from_agent: str,
        to: str,
        message: str,
        timeout: int = 300,
        require_approval: str = "auto",
    ) -> dict:
        return await self._post(
            "/api/route",
            {
                "from_agent": from_agent,
                "to_agent": to,
                "type": "ask",
                "payload": {
                    "message": message,
                    "timeout": timeout,
                    "require_approval": require_approval,
                },
            },
        )

    async def start_agent(
        self,
        from_agent: str,
        machine: str,
        project: str,
        mission: str,
        agent_command: str | None = None,
    ) -> dict:
        return await self._post(
            "/api/route",
            {
                "from_agent": from_agent,
                "to_agent": f"{machine}/{project}",
                "type": "start_agent",
                "payload": {"mission": mission, "agent_command": agent_command},
            },
        )

    async def get_status(self, mission_id: str) -> dict:
        return await self._get(f"/api/missions/{mission_id}")

    async def get_history(self, mission_id: str, limit: int = 50) -> dict:
        return await self._get(
            f"/api/missions/{mission_id}/history", {"limit": limit}
        )

    async def register(
        self,
        machine_id: str,
        project_id: str,
        action: str,
        machine_data: dict | None = None,
        project_data: dict | None = None,
    ) -> dict:
        return await self._post(
            "/api/register/update",
            {
                "machine_id": machine_id,
                "project_id": project_id,
                "action": action,
                "machine": machine_data,
                "project": project_data,
            },
        )
