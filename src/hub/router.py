"""Hub router: routes messages between agents.

Checks target machine status, evaluates approval policies,
posts to Telegram for visibility, and dispatches to daemons.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from src.hub.approval import ApprovalEngine, ApprovalLevel
from src.hub.registry import Registry
from src.shared.models import AgentId, Message


class Router:
    """Message router that coordinates registry lookup, approval, and dispatch."""

    def __init__(
        self,
        registry: Registry,
        approval_engine: ApprovalEngine,
        send_to_daemon: Callable[[str, dict, str], Awaitable[dict]],
        send_telegram: Callable[[Message], Awaitable[None]],
        request_approval: Callable[[Message], Awaitable[str | None]],
    ):
        self.registry = registry
        self.approval = approval_engine
        self.send_to_daemon = send_to_daemon
        self.send_telegram = send_telegram
        self.request_approval = request_approval

    async def route(self, msg: Message) -> dict[str, Any]:
        """Route a message to the target agent's daemon.

        Steps:
          1. Resolve target machine from registry
          2. Check machine status (online/offline/revoked)
          3. Evaluate approval policy; request human approval if needed
          4. Post to Telegram for visibility
          5. Dispatch to the target daemon
        """
        target = AgentId.from_string(msg.to_agent)

        # Check target machine status
        machine = await self.registry.get_machine(target.machine)
        if not machine:
            return {"status": "error", "error": f"Unknown machine: {target.machine}"}
        if machine["status"] == "offline":
            return {"status": "error", "error": f"Machine {target.machine} is offline"}
        if machine["status"] == "revoked":
            return {"status": "error", "error": f"Machine {target.machine} is revoked"}

        # Check approval
        level = self.approval.evaluate(msg)
        if level in (ApprovalLevel.ONCE, ApprovalLevel.MISSION, ApprovalLevel.SESSION):
            granted_str = await self.request_approval(msg)
            if granted_str is None:
                return {"status": "denied", "error": "Approval denied or timed out"}
            # Map callback string to ApprovalLevel for grant storage
            grant_map = {
                "once": None,  # No persistent grant
                "mission": ApprovalLevel.MISSION,
                "always": ApprovalLevel.ALWAYS_ALLOW,
            }
            grant_level = grant_map.get(granted_str)
            if grant_level:
                self.approval.grant(msg.mission_id, msg.from_agent, msg.to_agent, grant_level)

        # Post to Telegram for visibility
        await self.send_telegram(msg)

        # Dispatch to daemon
        result = await self.send_to_daemon(
            machine["daemon_url"],
            msg.model_dump(),
            machine["token"],
        )
        return result
