from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


class IntercomTools:
    """Business logic for intercom MCP tools, decoupled from transport."""

    def __init__(self, hub_client: Any, machine_id: str, current_project: str):
        self.hub_client = hub_client
        self.machine_id = machine_id
        self.current_project = current_project
        self._inbox_path: str | None = None
        self._session_id: str | None = None

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
        """Send message and launch remote agent. Returns immediately with mission_id.

        Use intercom_status(mission_id) to check for completion and get the output.
        """
        route_result = await self.hub_client.ask(
            from_agent=self.from_agent,
            to=to,
            message=message,
            timeout=timeout,
            require_approval=require_approval,
        )
        return route_result

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

    async def chat(self, to: str, message: str) -> dict:
        return await self.hub_client.route_chat(
            from_agent=self.from_agent,
            to=to,
            message=message,
        )

    async def reply(self, thread_id: str, message: str) -> dict:
        return await self.hub_client.route_reply(
            from_agent=self.from_agent,
            thread_id=thread_id,
            message=message,
        )

    async def check_inbox(self) -> dict:
        if not self._inbox_path:
            return {"messages": [], "count": 0}
        inbox = Path(self._inbox_path)
        if not inbox.exists():
            return {"messages": [], "count": 0}

        lines = inbox.read_text().strip().split("\n")
        unread = []
        all_messages = []
        for line in lines:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
                if not msg.get("read"):
                    unread.append(msg)
                    msg["read"] = True
                all_messages.append(msg)
            except json.JSONDecodeError:
                all_messages.append(line)

        # Rewrite file with read markers
        if unread:
            with open(inbox, "w") as f:
                for msg in all_messages:
                    if isinstance(msg, dict):
                        f.write(json.dumps(msg) + "\n")
                    else:
                        f.write(msg + "\n")

        return {"messages": unread, "count": len(unread)}


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

        Returns immediately with a mission_id. Use intercom_status(mission_id)
        to poll for completion and retrieve the agent's output.

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

        Returns mission status with output when completed. Poll this after
        intercom_ask to get the agent's response. Status values: "running",
        "completed", "failed", "launched".

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

    @mcp.tool()
    async def intercom_chat(to: str, message: str) -> dict:
        """Send a message to an agent's active session. Creates a conversation thread.

        Use intercom_list_agents() first to check if the target has an active session.
        If no active session exists, you'll get status "no_active_session" — use
        intercom_ask() instead to launch a new agent.

        Args:
            to: Target agent ID (machine/project).
            message: The message to send.
        """
        return await tools.chat(to=to, message=message)

    @mcp.tool()
    async def intercom_reply(thread_id: str, message: str) -> dict:
        """Reply to a message in an existing conversation thread.

        Use the thread_id from a received message (shown in inbox notifications).

        Args:
            thread_id: The thread ID to reply in.
            message: Your reply message.
        """
        return await tools.reply(thread_id=thread_id, message=message)

    @mcp.tool()
    async def intercom_check_inbox() -> dict:
        """Check for pending messages from other agents.

        Messages arrive automatically via hooks between tool calls, but you can
        also check manually with this tool (e.g. when asked to "check your mail").
        Returns unread messages and marks them as read.
        """
        return await tools.check_inbox()

    return mcp
