from __future__ import annotations

import json

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

    async def _post(self, path: str, data: dict, timeout: int = 120) -> dict:
        body = json.dumps(data).encode()
        headers = self._auth_headers(body)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.hub_url}{path}", content=body, headers=headers
            )
            return resp.json()

    async def _get(self, path: str, params: dict | None = None, timeout: int = 15) -> dict:
        async with httpx.AsyncClient(timeout=timeout) as client:
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

    async def get_mission_status(self, mission_id: str) -> dict:
        """Get mission status from Hub (push-model data)."""
        return await self._get(f"/api/missions/{mission_id}/status")

    async def push_feedback(
        self,
        mission_id: str,
        feedback: list[dict],
        turn_count: int,
        status: str,
    ) -> dict:
        """Push feedback batch to Hub for a running mission."""
        return await self._post(f"/api/missions/{mission_id}/feedback", {
            "machine_id": self.machine_id,
            "feedback": feedback,
            "turn_count": turn_count,
            "status": status,
        })

    async def push_result(
        self,
        mission_id: str,
        status: str,
        output: str | None,
        feedback: list[dict],
        started_at: str,
        finished_at: str | None,
        turn_count: int,
    ) -> dict:
        """Push final mission result to Hub."""
        return await self._post(f"/api/missions/{mission_id}/result", {
            "machine_id": self.machine_id,
            "status": status,
            "output": output,
            "feedback": feedback,
            "started_at": started_at,
            "finished_at": finished_at,
            "turn_count": turn_count,
        })

    async def submit_feedback(
        self,
        from_agent: str,
        feedback_type: str,
        description: str,
        context: str = "",
    ) -> dict:
        return await self._post(
            "/api/feedback",
            {
                "from_agent": from_agent,
                "type": feedback_type,
                "description": description,
                "context": context,
            },
        )

    async def route_chat(
        self,
        from_agent: str,
        to: str,
        message: str,
        thread_id: str | None = None,
    ) -> dict:
        import uuid

        payload: dict = {"message": message}
        if thread_id:
            payload["thread_id"] = thread_id
        else:
            payload["thread_id"] = f"t-{uuid.uuid4().hex[:6]}"
        return await self._post(
            "/api/route",
            {
                "from_agent": from_agent,
                "to_agent": to,
                "type": "chat",
                "payload": payload,
            },
        )

    async def route_reply(
        self,
        from_agent: str,
        thread_id: str,
        message: str,
    ) -> dict:
        return await self._post(
            "/api/route",
            {
                "from_agent": from_agent,
                "to_agent": "",  # Resolved by hub from thread context
                "type": "chat",
                "payload": {"message": message, "thread_id": thread_id},
            },
        )

    async def push_attention_event(self, event: dict) -> dict:
        """Push an attention state change event to the hub."""
        session = event.get("session")
        data = {
            "machine_id": self.machine_id,
            "event": {
                "type": event["type"],
                "session": session.model_dump() if hasattr(session, "model_dump") else session,
            },
        }
        return await self._post("/api/attention/event", data)

    async def trigger_upgrade(
        self, target: str = "all", version: str = ""
    ) -> dict:
        """Trigger network upgrade via Hub."""
        return await self._post("/api/upgrade", {
            "target": target,
            "version": version,
        }, timeout=180)

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
