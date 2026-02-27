from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

POLL_INTERVAL = 5  # seconds between status polls


class IntercomTools:
    """Business logic for intercom MCP tools, decoupled from transport."""

    def __init__(self, hub_client: Any, machine_id: str, current_project: str, poll_interval: float = POLL_INTERVAL):
        self.hub_client = hub_client
        self.machine_id = machine_id
        self.current_project = current_project
        self.poll_interval = poll_interval

    @property
    def from_agent(self) -> str:
        return f"{self.machine_id}/{self.current_project}"

    async def list_agents(self, filter: str = "all") -> dict:
        agents = await self.hub_client.list_agents(filter=filter)
        return {"agents": agents}

    async def send(self, to: str, message: str, priority: str = "normal") -> dict:
        return await self.hub_client.send_message(
            from_agent=self.from_agent,
            to=to,
            message=message,
            priority=priority,
        )

    async def ask(
        self,
        to: str,
        message: str,
        timeout: int = 300,
        require_approval: str = "auto",
    ) -> dict:
        """Send message, launch remote agent, then poll for completion."""
        # Step 1: Route the message (this triggers launch_background on daemon)
        route_result = await self.hub_client.ask(
            from_agent=self.from_agent,
            to=to,
            message=message,
            timeout=timeout,
            require_approval=require_approval,
        )

        mission_id = route_result.get("mission_id")
        if not mission_id:
            return route_result

        # If route already returned an error, don't poll
        if route_result.get("status") in ("error", "denied", "launch_failed"):
            return route_result

        # Step 2: Poll daemon-status until completed/failed or timeout
        elapsed = 0
        while elapsed < timeout:
            await asyncio.sleep(self.poll_interval)
            elapsed += self.poll_interval
            try:
                status = await self.hub_client.get_daemon_mission_status(mission_id)
                mission_status = status.get("status", "unknown")
                if mission_status in ("completed", "failed"):
                    return status
                if mission_status == "unreachable":
                    logger.warning("Daemon unreachable for mission %s", mission_id)
            except Exception as e:
                logger.warning("Poll error for %s: %s", mission_id, e)

        return {
            "mission_id": mission_id,
            "status": "timeout",
            "output": f"No response after {timeout}s",
        }

    async def start_agent(
        self,
        machine: str,
        project: str,
        mission: str,
        agent_command: str | None = None,
    ) -> dict:
        return await self.hub_client.start_agent(
            from_agent=self.from_agent,
            machine=machine,
            project=project,
            mission=mission,
            agent_command=agent_command,
        )

    async def status(self, mission_id: str) -> dict:
        return await self.hub_client.get_status(mission_id=mission_id)

    async def daemon_status(self, mission_id: str) -> dict:
        return await self.hub_client.get_daemon_mission_status(mission_id=mission_id)

    async def history(self, mission_id: str, limit: int = 50) -> dict:
        return await self.hub_client.get_history(
            mission_id=mission_id, limit=limit
        )

    async def register(
        self,
        action: str = "update",
        machine: dict | None = None,
        project: dict | None = None,
    ) -> dict:
        return await self.hub_client.register(
            machine_id=self.machine_id,
            project_id=self.current_project,
            action=action,
            machine_data=machine,
            project_data=project,
        )

    async def report_feedback(
        self,
        feedback_type: str,
        description: str,
        context: str = "",
    ) -> dict:
        return await self.hub_client.submit_feedback(
            from_agent=self.from_agent,
            feedback_type=feedback_type,
            description=description,
            context=context,
        )


def create_mcp_server(tools: IntercomTools) -> FastMCP:
    """Create an MCP server exposing intercom tools."""

    mcp = FastMCP("ai-intercom")

    @mcp.tool()
    async def intercom_list_agents(filter: str = "all") -> dict:
        """List available agents on the intercom network.

        Args:
            filter: Filter agents - "all", "online", or "machine:<id>"
        """
        return await tools.list_agents(filter=filter)

    @mcp.tool()
    async def intercom_send(to: str, message: str, priority: str = "normal") -> dict:
        """Send a fire-and-forget message to another agent.

        Args:
            to: Target agent ID (machine/project). Use intercom_list_agents to discover.
            message: The message to send.
            priority: Message priority - "normal" or "high".
        """
        return await tools.send(to=to, message=message, priority=priority)

    @mcp.tool()
    async def intercom_ask(
        to: str,
        message: str,
        timeout: int = 300,
        require_approval: str = "auto",
    ) -> dict:
        """Send a message and wait for a response from another agent.

        Args:
            to: Target agent ID (machine/project). Use intercom_list_agents to discover.
            message: The message/mission to send.
            timeout: Max seconds to wait for response.
            require_approval: "auto" (use policy), "always", or "never".
        """
        return await tools.ask(
            to=to, message=message, timeout=timeout, require_approval=require_approval
        )

    @mcp.tool()
    async def intercom_start_agent(
        machine: str,
        project: str,
        mission: str,
        agent_command: str = "",
    ) -> dict:
        """Start an AI agent on a remote machine.

        Args:
            machine: Target machine ID.
            project: Project ID on that machine.
            mission: The mission/task for the agent.
            agent_command: Override default agent command (e.g. "claude", "codex").
        """
        return await tools.start_agent(
            machine=machine,
            project=project,
            mission=mission,
            agent_command=agent_command or None,
        )

    @mcp.tool()
    async def intercom_status(mission_id: str) -> dict:
        """Get the status of a running mission.

        Args:
            mission_id: The mission ID to check.
        """
        # Try daemon-level status first (has output), fall back to hub history
        try:
            result = await tools.daemon_status(mission_id=mission_id)
            if result.get("status") != "unreachable":
                return result
        except Exception:
            pass
        return await tools.status(mission_id=mission_id)

    @mcp.tool()
    async def intercom_history(mission_id: str, limit: int = 50) -> dict:
        """Get the full conversation history of a mission.

        Args:
            mission_id: The mission ID.
            limit: Max messages to return.
        """
        return await tools.history(mission_id=mission_id, limit=limit)

    @mcp.tool()
    async def intercom_register(
        action: str = "update",
        machine: dict | None = None,
        project: dict | None = None,
    ) -> dict:
        """Update this agent's registry entry (description, capabilities, etc).

        Args:
            action: "update", "add_project", or "remove_project".
            machine: Machine metadata to update (description, capabilities).
            project: Project metadata to update (description, capabilities, tags).
        """
        return await tools.register(action=action, machine=machine, project=project)

    @mcp.tool()
    async def intercom_report_feedback(
        type: str,
        description: str,
        context: str = "",
    ) -> dict:
        """Report feedback, bugs, or improvement suggestions to the intercom system.

        Args:
            type: Feedback type - "bug", "improvement", or "note".
            description: Description of the feedback.
            context: Optional additional context (error messages, logs, etc).
        """
        return await tools.report_feedback(
            feedback_type=type, description=description, context=context
        )

    return mcp
