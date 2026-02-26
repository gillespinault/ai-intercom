"""Approval engine: evaluates messages against policy rules.

Determines whether a message needs human approval (via Telegram)
before delivery. Supports static rules with glob/regex matching
and runtime grants (mission-level, session-level).
"""

from __future__ import annotations

import re
from enum import StrEnum
from fnmatch import fnmatch
from typing import Any

from src.shared.models import Message


class ApprovalLevel(StrEnum):
    NEVER = "never"
    ALWAYS_ALLOW = "always_allow"
    ONCE = "once"
    MISSION = "mission"
    SESSION = "session"


class ApprovalEngine:
    """Policy-based approval engine with runtime grant support.

    Evaluation priority:
      1. Mission grants (exact mission_id + from + to)
      2. Session grants (from + to pair)
      3. Static rules (first match wins)
      4. Default policy
    """

    def __init__(self, policies: dict[str, Any]) -> None:
        self.defaults = policies.get("defaults", {})
        self.rules: list[dict] = policies.get("rules", [])
        # Runtime grants: (mission_id, from, to) -> level
        self._grants: dict[tuple[str, str, str], ApprovalLevel] = {}
        # Session grants: (from, to) -> level
        self._session_grants: dict[tuple[str, str], ApprovalLevel] = {}

    def evaluate(self, msg: Message) -> ApprovalLevel:
        """Evaluate a message and return the required approval level."""
        # Check runtime grants first (mission-level)
        grant_key = (msg.mission_id, msg.from_agent, msg.to_agent)
        if grant_key in self._grants:
            return self._grants[grant_key]

        # Check session grants
        session_key = (msg.from_agent, msg.to_agent)
        if session_key in self._session_grants:
            return self._session_grants[session_key]

        # Evaluate static rules (first match wins)
        for rule in self.rules:
            if self._matches(rule, msg):
                return ApprovalLevel(rule["approval"])

        # Default
        return ApprovalLevel(self.defaults.get("require_approval", "once"))

    def grant(
        self,
        mission_id: str,
        from_agent: str,
        to_agent: str,
        level: ApprovalLevel,
    ) -> None:
        """Record a runtime approval grant."""
        if level == ApprovalLevel.MISSION:
            self._grants[(mission_id, from_agent, to_agent)] = level
        elif level == ApprovalLevel.SESSION:
            self._session_grants[(from_agent, to_agent)] = level
        elif level == ApprovalLevel.ALWAYS_ALLOW:
            self._session_grants[(from_agent, to_agent)] = level

    def clear_mission_grants(self, mission_id: str) -> None:
        """Remove all grants associated with a mission."""
        to_remove = [k for k in self._grants if k[0] == mission_id]
        for k in to_remove:
            del self._grants[k]

    def _matches(self, rule: dict, msg: Message) -> bool:
        """Check if a rule matches a message."""
        if not fnmatch(msg.from_agent, rule.get("from", "*")):
            return False
        if not fnmatch(msg.to_agent, rule.get("to", "*")):
            return False
        rule_type = rule.get("type", "*")
        if rule_type != "*" and rule_type != msg.type:
            return False
        pattern = rule.get("message_pattern")
        if pattern:
            message_text = msg.payload.get("message", "")
            if not re.search(pattern, message_text, re.IGNORECASE):
                return False
        return True
