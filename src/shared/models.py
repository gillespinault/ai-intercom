from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class MessageType(StrEnum):
    ASK = "ask"
    SEND = "send"
    RESPONSE = "response"
    START_AGENT = "start_agent"
    STATUS = "status"


class AgentStatus(StrEnum):
    ONLINE = "online"
    AWAY = "away"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class AgentId(BaseModel):
    machine: str
    project: str

    @classmethod
    def from_string(cls, value: str) -> AgentId:
        parts = value.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"Invalid agent ID: {value!r}. Expected 'machine/project'.")
        return cls(machine=parts[0], project=parts[1])

    def __str__(self) -> str:
        return f"{self.machine}/{self.project}"


class Message(BaseModel):
    version: str = "1"
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    mission_id: str = Field(
        default_factory=lambda: (
            f"m-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        )
    )
    from_agent: str
    to_agent: str
    type: MessageType
    payload: dict
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @field_validator("from_agent", "to_agent")
    @classmethod
    def validate_agent_id(cls, v: str) -> str:
        if v != "human":
            AgentId.from_string(v)
        return v


class AgentInfo(BaseModel):
    machine: str
    project: str
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    agent_command: str = "claude"
    path: str = ""
    status: AgentStatus = AgentStatus.UNKNOWN
    agent_active: bool = False

    @property
    def id(self) -> str:
        return f"{self.machine}/{self.project}"


class MachineInfo(BaseModel):
    id: str
    display_name: str = ""
    description: str = ""
    tailscale_ip: str = ""
    daemon_url: str = ""
    token_hash: str = ""
    status: AgentStatus = AgentStatus.UNKNOWN
    last_seen: str | None = None
    projects: list[AgentInfo] = Field(default_factory=list)
