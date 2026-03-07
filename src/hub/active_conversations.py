"""Track active dispatcher conversations per Telegram user."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

CONVERSATION_TTL = 600  # 10 minutes


@dataclass
class ActiveConversation:
    user_id: int
    mission_id: str
    daemon_url: str
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    status: Literal["active", "completed", "failed"] = "active"


class ActiveConversationManager:
    """Tracks one active conversation per user."""

    def __init__(self) -> None:
        self._active: dict[int, ActiveConversation] = {}

    def start(self, user_id: int, mission_id: str, daemon_url: str) -> None:
        self._active[user_id] = ActiveConversation(
            user_id=user_id,
            mission_id=mission_id,
            daemon_url=daemon_url,
        )

    def get_active(self, user_id: int) -> ActiveConversation | None:
        conv = self._active.get(user_id)
        if conv and conv.status == "active":
            return conv
        return None

    def touch(self, user_id: int) -> None:
        conv = self._active.get(user_id)
        if conv:
            conv.last_activity = time.time()

    def close(self, user_id: int) -> None:
        self._active.pop(user_id, None)

    def cleanup_stale(self, ttl: int = CONVERSATION_TTL) -> None:
        now = time.time()
        stale = [
            uid for uid, conv in self._active.items()
            if now - conv.last_activity > ttl
        ]
        for uid in stale:
            logger.info(
                "Closing stale conversation for user %d (mission %s)",
                uid, self._active[uid].mission_id,
            )
            del self._active[uid]

    def is_injectable(self, user_id: int) -> bool:
        """Check if a message can be injected into the active conversation."""
        conv = self.get_active(user_id)
        if not conv:
            return False
        return time.time() - conv.last_activity < CONVERSATION_TTL
