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
    CHAT = "chat"


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


class SessionInfo(BaseModel):
    """Represents an active Claude Code session on a daemon."""
    session_id: str
    project: str
    pid: int
    inbox_path: str
    registered_at: str = ""
    status: str = "active"
    summary: str = ""
    recent_activity: list[str] = Field(default_factory=list)


class ThreadMessage(BaseModel):
    """A single message in an inter-agent chat thread."""
    thread_id: str
    from_agent: str
    timestamp: str
    message: str
    read: bool = False


# ---------------------------------------------------------------------------
# Attention Hub models
# ---------------------------------------------------------------------------


class AttentionState(StrEnum):
    WORKING = "working"
    THINKING = "thinking"
    WAITING = "waiting"
    ENDED = "ended"


class PromptType(StrEnum):
    PERMISSION = "permission"
    QUESTION = "question"
    TEXT_INPUT = "text_input"


class PromptChoice(BaseModel):
    key: str
    label: str


class DetectedPrompt(BaseModel):
    type: PromptType
    raw_text: str = ""
    tool: str | None = None
    command_preview: str | None = None
    question: str | None = None
    choices: list[PromptChoice] = Field(default_factory=list)
    allows_free_text: bool = False


class AttentionHeartbeat(BaseModel):
    """Written by PostToolUse hook to /tmp/cc-sessions/{pid}."""
    pid: int
    session_id: str
    session_name: str = ""
    machine: str
    project: str
    last_tool: str = ""
    last_tool_time: str = ""
    tmux_session: str = ""
    rc_url: str | None = None


class AttentionSession(BaseModel):
    """Aggregated session state tracked by the hub."""
    session_id: str
    machine: str
    project: str
    session_name: str = ""
    pid: int
    state: AttentionState = AttentionState.WORKING
    state_since: str = ""
    last_tool: str = ""
    last_tool_time: str = ""
    rc_url: str | None = None
    idle_seconds: int = 0
    prompt: DetectedPrompt | None = None
    tmux_session: str = ""


class AttentionEvent(BaseModel):
    """Event sent over WebSocket to the PWA."""
    type: str
    session: AttentionSession | None = None
    sessions: list[AttentionSession] | None = None
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
