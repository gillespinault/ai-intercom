# Interactive Agent-to-Agent Chat — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable asynchronous bidirectional chat between active Claude Code sessions across machines, using PostToolUse hooks and file-based inbox delivery.

**Architecture:** Sessions register with their local daemon on MCP server startup. Chat messages route through the hub to the target daemon, which writes to the session's inbox file. PostToolUse/UserPromptSubmit hooks read the inbox and inject messages into the agent's context. Agents reply via `intercom_reply()`.

**Tech Stack:** Python 3.12, FastAPI, FastMCP, JSONL files, Claude Code hooks (shell scripts)

**Design doc:** `docs/plans/2026-02-28-interactive-chat-design.md`

---

### Task 1: Add Models (SessionInfo, ThreadMessage, MessageType.CHAT)

**Files:**
- Modify: `src/shared/models.py:10-16` (MessageType enum)
- Modify: `src/shared/models.py` (add new models at end)
- Test: `tests/test_shared/test_models.py`

**Step 1: Write failing tests**

```python
# tests/test_shared/test_models.py — append to existing file

def test_message_type_chat():
    assert MessageType.CHAT == "chat"


def test_session_info_defaults():
    s = SessionInfo(session_id="s-123", project="myproj", pid=999, inbox_path="/tmp/inbox.jsonl")
    assert s.status == "active"
    assert s.summary == ""
    assert s.recent_activity == []
    assert s.registered_at == ""


def test_thread_message_defaults():
    m = ThreadMessage(
        thread_id="t-abc",
        from_agent="limn/mnemos",
        timestamp="2026-02-28T16:00:00Z",
        message="hello",
    )
    assert m.read is False


def test_thread_message_read():
    m = ThreadMessage(
        thread_id="t-abc",
        from_agent="limn/mnemos",
        timestamp="2026-02-28T16:00:00Z",
        message="hello",
        read=True,
    )
    assert m.read is True
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_shared/test_models.py -v -k "chat or session_info or thread_message"`
Expected: FAIL — `MessageType.CHAT`, `SessionInfo`, `ThreadMessage` not defined

**Step 3: Implement models**

Add `CHAT = "chat"` to `MessageType` enum at line 15:

```python
class MessageType(StrEnum):
    ASK = "ask"
    SEND = "send"
    RESPONSE = "response"
    START_AGENT = "start_agent"
    STATUS = "status"
    CHAT = "chat"
```

Add new models after `MachineInfo` (end of file):

```python
class SessionInfo(BaseModel):
    """Represents an active Claude Code session on a daemon."""
    session_id: str
    project: str
    pid: int
    inbox_path: str
    registered_at: str = ""
    status: str = "active"  # active, working, idle
    summary: str = ""
    recent_activity: list[str] = Field(default_factory=list)


class ThreadMessage(BaseModel):
    """A single message in an inter-agent chat thread."""
    thread_id: str
    from_agent: str
    timestamp: str
    message: str
    read: bool = False
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_shared/test_models.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/shared/models.py tests/test_shared/test_models.py
git commit -m "feat: add SessionInfo, ThreadMessage models and MessageType.CHAT"
```

---

### Task 2: Daemon session registry (register/unregister/deliver endpoints)

**Files:**
- Modify: `src/daemon/api.py:15-108` (add session endpoints to `create_app`)
- Test: `tests/test_daemon/test_api.py`

**Step 1: Write failing tests**

```python
# tests/test_daemon/test_api.py — append to existing tests

import json
import os
import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.daemon.api import create_app


@pytest.fixture
def app_with_sessions():
    app = create_app(machine_id="test-machine", token="test-token")
    return app


@pytest.fixture
def client_sessions(app_with_sessions):
    return TestClient(app_with_sessions)


def test_session_register(client_sessions):
    resp = client_sessions.post("/api/session/register", json={
        "session_id": "s-test-001",
        "project": "myproj",
        "pid": os.getpid(),
        "inbox_path": "/tmp/test-inbox.jsonl",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "registered"


def test_session_register_then_list(client_sessions):
    client_sessions.post("/api/session/register", json={
        "session_id": "s-test-002",
        "project": "proj2",
        "pid": os.getpid(),
        "inbox_path": "/tmp/test-inbox2.jsonl",
    })
    resp = client_sessions.get("/api/sessions")
    data = resp.json()
    assert len(data["sessions"]) == 1
    assert data["sessions"][0]["project"] == "proj2"


def test_session_unregister(client_sessions):
    client_sessions.post("/api/session/register", json={
        "session_id": "s-test-003",
        "project": "proj3",
        "pid": os.getpid(),
        "inbox_path": "/tmp/test.jsonl",
    })
    resp = client_sessions.post("/api/session/unregister", json={
        "session_id": "s-test-003",
    })
    assert resp.json()["status"] == "unregistered"
    resp = client_sessions.get("/api/sessions")
    assert len(resp.json()["sessions"]) == 0


def test_session_deliver(client_sessions):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        inbox_path = f.name

    try:
        client_sessions.post("/api/session/register", json={
            "session_id": "s-test-004",
            "project": "myproj",
            "pid": os.getpid(),
            "inbox_path": inbox_path,
        })
        resp = client_sessions.post("/api/session/deliver", json={
            "project": "myproj",
            "thread_id": "t-abc123",
            "from_agent": "limn/mnemos",
            "message": "Hello from limn!",
            "timestamp": "2026-02-28T16:00:00Z",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "delivered"

        # Verify inbox file
        with open(inbox_path) as f:
            line = f.readline()
        data = json.loads(line)
        assert data["message"] == "Hello from limn!"
        assert data["read"] is False
    finally:
        os.unlink(inbox_path)


def test_session_deliver_no_session(client_sessions):
    resp = client_sessions.post("/api/session/deliver", json={
        "project": "nonexistent",
        "thread_id": "t-xxx",
        "from_agent": "limn/mnemos",
        "message": "Hello?",
        "timestamp": "2026-02-28T16:00:00Z",
    })
    assert resp.status_code == 404
    assert resp.json()["status"] == "no_active_session"


def test_session_deliver_dead_pid(client_sessions):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        inbox_path = f.name

    try:
        client_sessions.post("/api/session/register", json={
            "session_id": "s-test-005",
            "project": "deadproj",
            "pid": 999999,  # PID that doesn't exist
            "inbox_path": inbox_path,
        })
        resp = client_sessions.post("/api/session/deliver", json={
            "project": "deadproj",
            "thread_id": "t-xxx",
            "from_agent": "limn/mnemos",
            "message": "Hello?",
            "timestamp": "2026-02-28T16:00:00Z",
        })
        assert resp.status_code == 404
        assert resp.json()["status"] == "no_active_session"
    finally:
        os.unlink(inbox_path)


def test_session_status(client_sessions):
    client_sessions.post("/api/session/register", json={
        "session_id": "s-test-006",
        "project": "proj6",
        "pid": os.getpid(),
        "inbox_path": "/tmp/test6.jsonl",
    })
    resp = client_sessions.get("/api/session/s-test-006/status")
    assert resp.status_code == 200
    assert resp.json()["project"] == "proj6"
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_daemon/test_api.py -v -k "session"`
Expected: FAIL — endpoints don't exist

**Step 3: Implement daemon session endpoints**

In `src/daemon/api.py`, inside `create_app()`, add after `app.state.launcher = None`:

```python
    app.state.active_sessions: dict[str, dict] = {}  # session_id → session info

    @app.post("/api/session/register")
    async def session_register(request: Request):
        data = await request.json()
        session_id = data["session_id"]
        app.state.active_sessions[session_id] = {
            "session_id": session_id,
            "project": data["project"],
            "pid": data["pid"],
            "inbox_path": data["inbox_path"],
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "status": "active",
            "summary": "",
            "recent_activity": [],
        }
        # Ensure inbox directory exists
        Path(data["inbox_path"]).parent.mkdir(parents=True, exist_ok=True)
        return {"status": "registered", "session_id": session_id}

    @app.post("/api/session/unregister")
    async def session_unregister(request: Request):
        data = await request.json()
        session_id = data["session_id"]
        removed = app.state.active_sessions.pop(session_id, None)
        if removed:
            # Clean up inbox file
            inbox = Path(removed["inbox_path"])
            if inbox.exists():
                inbox.unlink()
        return {"status": "unregistered", "session_id": session_id}

    @app.get("/api/sessions")
    async def list_sessions():
        return {"sessions": list(app.state.active_sessions.values())}

    @app.post("/api/session/deliver")
    async def session_deliver(request: Request):
        data = await request.json()
        project = data.get("project", "")
        session_id = data.get("session_id")

        # Find session by session_id or project
        session = None
        if session_id:
            session = app.state.active_sessions.get(session_id)
        else:
            for s in app.state.active_sessions.values():
                if s["project"] == project:
                    session = s
                    break

        if not session:
            return Response(
                status_code=404,
                content=json.dumps({"status": "no_active_session"}),
                media_type="application/json",
            )

        # Verify PID is alive
        import os, signal
        try:
            os.kill(session["pid"], 0)
        except (ProcessLookupError, PermissionError):
            # PID dead — clean up session
            app.state.active_sessions.pop(session["session_id"], None)
            return Response(
                status_code=404,
                content=json.dumps({"status": "no_active_session"}),
                media_type="application/json",
            )

        # Append message to inbox
        inbox_entry = {
            "thread_id": data["thread_id"],
            "from_agent": data["from_agent"],
            "timestamp": data["timestamp"],
            "message": data["message"],
            "read": False,
        }
        inbox_path = Path(session["inbox_path"])
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        with open(inbox_path, "a") as f:
            f.write(json.dumps(inbox_entry) + "\n")

        return {"status": "delivered", "session_id": session["session_id"]}

    @app.get("/api/session/{session_id}/status")
    async def session_status(session_id: str):
        session = app.state.active_sessions.get(session_id)
        if not session:
            return Response(status_code=404, content="Session not found")
        # Count pending inbox messages
        inbox_pending = 0
        inbox_path = Path(session["inbox_path"])
        if inbox_path.exists():
            for line in inbox_path.read_text().strip().split("\n"):
                if line.strip():
                    try:
                        msg = json.loads(line)
                        if not msg.get("read"):
                            inbox_pending += 1
                    except json.JSONDecodeError:
                        pass
        return {
            **session,
            "inbox_pending": inbox_pending,
        }
```

Add required imports at top of `api.py`:

```python
from datetime import datetime, timezone
from pathlib import Path
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_daemon/test_api.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/daemon/api.py tests/test_daemon/test_api.py
git commit -m "feat: add daemon session register/unregister/deliver endpoints"
```

---

### Task 3: MCP server session lifecycle and new tools

**Files:**
- Modify: `src/daemon/mcp_server.py` (add methods to `IntercomTools`, add MCP tools to `create_mcp_server`)
- Modify: `src/cli.py:73-87` (session registration on MCP startup)
- Test: `tests/test_daemon/test_mcp_server.py`

**Step 1: Write failing tests**

```python
# tests/test_daemon/test_mcp_server.py — append

import json
import os
import tempfile

import pytest

from src.daemon.mcp_server import IntercomTools


class FakeHubClientChat:
    """Fake hub client that also handles chat operations."""
    def __init__(self):
        self.last_route = None

    async def list_agents(self, filter="all"):
        return [{"machine_id": "limn", "project_id": "mnemos", "session": None}]

    async def route_chat(self, from_agent, to, message, thread_id=None):
        self.last_route = {"from": from_agent, "to": to, "message": message, "thread_id": thread_id}
        return {"status": "delivered", "thread_id": thread_id or "t-new123"}

    async def route_reply(self, from_agent, thread_id, message):
        self.last_route = {"from": from_agent, "thread_id": thread_id, "message": message}
        return {"status": "delivered", "thread_id": thread_id}


@pytest.fixture
def chat_tools():
    client = FakeHubClientChat()
    tools = IntercomTools(client, "serverlab", "ai-intercom")
    return tools, client


@pytest.mark.asyncio
async def test_chat_sends_via_hub(chat_tools):
    tools, client = chat_tools
    result = await tools.chat(to="limn/mnemos", message="hello")
    assert result["status"] == "delivered"
    assert client.last_route["to"] == "limn/mnemos"
    assert client.last_route["message"] == "hello"


@pytest.mark.asyncio
async def test_reply_sends_via_hub(chat_tools):
    tools, client = chat_tools
    result = await tools.reply(thread_id="t-abc", message="world")
    assert result["status"] == "delivered"
    assert client.last_route["thread_id"] == "t-abc"


@pytest.mark.asyncio
async def test_check_inbox_empty(chat_tools):
    tools, _ = chat_tools
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        inbox_path = f.name
    try:
        tools._inbox_path = inbox_path
        result = await tools.check_inbox()
        assert result["count"] == 0
        assert result["messages"] == []
    finally:
        os.unlink(inbox_path)


@pytest.mark.asyncio
async def test_check_inbox_with_messages(chat_tools):
    tools, _ = chat_tools
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({
            "thread_id": "t-abc",
            "from_agent": "limn/mnemos",
            "timestamp": "2026-02-28T16:00:00Z",
            "message": "hello",
            "read": False,
        }) + "\n")
        inbox_path = f.name
    try:
        tools._inbox_path = inbox_path
        result = await tools.check_inbox()
        assert result["count"] == 1
        assert result["messages"][0]["message"] == "hello"

        # Verify message marked as read
        with open(inbox_path) as f:
            data = json.loads(f.readline())
        assert data["read"] is True
    finally:
        os.unlink(inbox_path)
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_daemon/test_mcp_server.py -v -k "chat or reply or inbox"`
Expected: FAIL — `chat`, `reply`, `check_inbox` methods don't exist

**Step 3: Implement IntercomTools methods**

In `src/daemon/mcp_server.py`, add to `IntercomTools.__init__`:

```python
    self._inbox_path: str | None = None
    self._session_id: str | None = None
```

Add methods to `IntercomTools` class:

```python
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
```

Add imports at top of `mcp_server.py`:

```python
import json
from pathlib import Path
```

Add MCP tool registrations in `create_mcp_server()` before `return mcp`:

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_daemon/test_mcp_server.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/daemon/mcp_server.py tests/test_daemon/test_mcp_server.py
git commit -m "feat: add intercom_chat, intercom_reply, intercom_check_inbox MCP tools"
```

---

### Task 4: HubClient chat methods

**Files:**
- Modify: `src/daemon/hub_client.py` (add `route_chat` and `route_reply` methods)
- Test: `tests/test_daemon/test_hub_client.py`

**Step 1: Write failing tests**

```python
# tests/test_daemon/test_hub_client.py — append

import pytest

from src.daemon.hub_client import HubClient


@pytest.mark.asyncio
async def test_route_chat(httpx_mock):
    httpx_mock.add_response(
        url="http://hub:7700/api/route",
        json={"status": "delivered", "thread_id": "t-new1", "mission_id": "m-chat-001"},
    )
    client = HubClient("http://hub:7700", "token", "serverlab")
    result = await client.route_chat(
        from_agent="serverlab/ai-intercom",
        to="limn/mnemos",
        message="hello",
    )
    assert result["status"] == "delivered"
    assert result["thread_id"] == "t-new1"


@pytest.mark.asyncio
async def test_route_reply(httpx_mock):
    httpx_mock.add_response(
        url="http://hub:7700/api/route",
        json={"status": "delivered", "thread_id": "t-existing"},
    )
    client = HubClient("http://hub:7700", "token", "serverlab")
    result = await client.route_reply(
        from_agent="serverlab/ai-intercom",
        thread_id="t-existing",
        message="reply here",
    )
    assert result["status"] == "delivered"
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_daemon/test_hub_client.py -v -k "route_chat or route_reply"`
Expected: FAIL — methods don't exist

**Step 3: Implement HubClient methods**

Add to `HubClient` class in `src/daemon/hub_client.py`:

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_daemon/test_hub_client.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/daemon/hub_client.py tests/test_daemon/test_hub_client.py
git commit -m "feat: add route_chat and route_reply to HubClient"
```

---

### Task 5: Hub routing for chat messages

**Files:**
- Modify: `src/hub/hub_api.py:269-315` (add chat routing in `route_message`)
- Test: `tests/test_hub/test_hub_api.py`

**Step 1: Write failing tests**

```python
# tests/test_hub/test_hub_api.py — append

@pytest.mark.asyncio
async def test_route_chat_delivered(client, httpx_mock):
    """Chat message delivered to daemon with active session."""
    # Register a machine first
    await app.state.registry.register_machine(
        machine_id="limn", display_name="Limn", tailscale_ip="100.0.0.2",
        daemon_url="http://100.0.0.2:7700", token="",
    )
    await app.state.registry.register_project(
        machine_id="limn", project_id="mnemos", description="", capabilities=[], path="",
    )

    # Mock daemon session deliver endpoint
    httpx_mock.add_response(
        url="http://100.0.0.2:7700/api/session/deliver",
        json={"status": "delivered", "session_id": "s-123"},
    )

    resp = client.post("/api/route", json={
        "from_agent": "serverlab/ai-intercom",
        "to_agent": "limn/mnemos",
        "type": "chat",
        "payload": {"message": "hello", "thread_id": "t-abc"},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "delivered"


@pytest.mark.asyncio
async def test_route_chat_no_session(client, httpx_mock):
    """Chat message returns no_active_session when daemon has no session."""
    await app.state.registry.register_machine(
        machine_id="limn", display_name="Limn", tailscale_ip="100.0.0.2",
        daemon_url="http://100.0.0.2:7700", token="",
    )
    await app.state.registry.register_project(
        machine_id="limn", project_id="mnemos", description="", capabilities=[], path="",
    )

    httpx_mock.add_response(
        url="http://100.0.0.2:7700/api/session/deliver",
        status_code=404,
        json={"status": "no_active_session"},
    )

    resp = client.post("/api/route", json={
        "from_agent": "serverlab/ai-intercom",
        "to_agent": "limn/mnemos",
        "type": "chat",
        "payload": {"message": "hello", "thread_id": "t-abc"},
    })
    data = resp.json()
    assert data["status"] == "no_active_session"
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_hub_api.py -v -k "route_chat"`
Expected: FAIL — chat messages route through normal mission flow, not session delivery

**Step 3: Implement chat routing**

In `src/hub/hub_api.py`, modify `route_message()` to handle `type="chat"` before the existing router logic. Add after `app.state.mission_store[mission_id].append(msg.model_dump())` (line 293):

```python
        # Handle chat messages: deliver to active session, don't launch agent
        if msg.type == "chat":
            to_agent = data.get("to_agent", "")
            target_machine = to_agent.split("/")[0] if "/" in to_agent else to_agent
            target_project = to_agent.split("/", 1)[1] if "/" in to_agent else ""
            machine = await registry.get_machine(target_machine)

            thread_id = data.get("payload", {}).get("thread_id", "")

            # Store thread mapping for replies
            if thread_id:
                if not hasattr(app.state, "thread_store"):
                    app.state.thread_store = {}
                if thread_id not in app.state.thread_store:
                    app.state.thread_store[thread_id] = {
                        "participants": [from_agent, to_agent],
                    }

            if not machine:
                return {"status": "error", "error": f"Machine {target_machine} not found"}

            # Try to deliver to active session on daemon
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"{machine['daemon_url']}/api/session/deliver",
                        json={
                            "project": target_project,
                            "thread_id": thread_id,
                            "from_agent": from_agent,
                            "message": data.get("payload", {}).get("message", ""),
                            "timestamp": msg.timestamp,
                        },
                    )
                    if resp.status_code == 404:
                        # Notify Telegram
                        if telegram_bot:
                            await telegram_bot.post_text_to_mission(
                                mission_id,
                                f"📨 Chat `{from_agent}` → `{to_agent}`\n"
                                f"⚠️ _Pas de session active_",
                            )
                        return {"status": "no_active_session", "thread_id": thread_id}

                    result = resp.json()
                    # Notify Telegram
                    if telegram_bot:
                        msg_text = data.get("payload", {}).get("message", "")
                        preview = msg_text[:200] + "..." if len(msg_text) > 200 else msg_text
                        await telegram_bot.post_text_to_mission(
                            mission_id,
                            f"📨 Chat [{thread_id}]\n"
                            f"`{from_agent}` → `{to_agent}`\n\n"
                            f"_{preview}_\n\n"
                            f"Session: ✅ délivré",
                        )
                    return {"status": "delivered", "thread_id": thread_id, "mission_id": mission_id}
            except Exception as e:
                logger.error("Chat delivery failed: %s", e)
                return {"status": "error", "error": str(e), "thread_id": thread_id}
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_hub_api.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/hub/hub_api.py tests/test_hub/test_hub_api.py
git commit -m "feat: hub routes chat messages to daemon active sessions"
```

---

### Task 6: Enriched heartbeat with active sessions

**Files:**
- Modify: `src/daemon/main.py:180-201` (heartbeat loop sends active_sessions)
- Modify: `src/hub/hub_api.py:237-265` (heartbeat endpoint stores sessions)
- Modify: `src/hub/hub_api.py:488-493` (list_agents enriched)

**Step 1: Modify daemon heartbeat to include sessions**

In `src/daemon/main.py`, modify `_heartbeat_loop` to include active sessions. The heartbeat body (line 190-194) becomes:

```python
            # Collect active sessions from daemon API
            active_sessions = []
            # Access through module-level reference set during run_daemon
            if _daemon_app and hasattr(_daemon_app.state, "active_sessions"):
                for s in _daemon_app.state.active_sessions.values():
                    active_sessions.append({
                        "session_id": s["session_id"],
                        "project": s["project"],
                        "status": s.get("status", "active"),
                        "summary": s.get("summary", ""),
                    })

            body = json.dumps({
                "machine_id": machine_id,
                "tailscale_ip": tailscale_ip,
                "daemon_url": daemon_url,
                "active_sessions": active_sessions,
            }).encode()
```

Add module-level variable at top of `main.py`: `_daemon_app = None` and set it in `run_daemon`: `_daemon_app = app`.

**Step 2: Hub stores sessions from heartbeat**

In `src/hub/hub_api.py`, in the `heartbeat` handler, after `await registry.update_heartbeat(...)` add:

```python
        # Store active sessions from daemon
        active_sessions = data.get("active_sessions", [])
        if not hasattr(app.state, "machine_sessions"):
            app.state.machine_sessions = {}
        app.state.machine_sessions[machine_id] = active_sessions
```

**Step 3: Enrich list_agents response**

In `list_agents` endpoint, after building the agents list, enrich with session info:

```python
        # Enrich with active session info
        machine_sessions = getattr(app.state, "machine_sessions", {})
        for agent in agents:
            mid = agent.get("machine_id", "")
            pid = agent.get("project_id", "")
            sessions = machine_sessions.get(mid, [])
            session = next((s for s in sessions if s["project"] == pid), None)
            agent["session"] = session
```

**Step 4: Run all tests**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/daemon/main.py src/hub/hub_api.py
git commit -m "feat: heartbeat includes active sessions, list_agents enriched"
```

---

### Task 7: MCP server session registration on startup/shutdown

**Files:**
- Modify: `src/cli.py:73-87` (register session on MCP server start)
- Modify: `src/daemon/mcp_server.py` (set inbox_path)

**Step 1: Implement session registration in CLI**

In `src/cli.py`, after `tools = IntercomTools(...)` and before `mcp.run()`, add session registration:

```python
        import uuid
        import atexit

        session_id = f"s-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        inbox_dir = Path(os.path.expanduser("~/.config/ai-intercom/inbox"))
        inbox_dir.mkdir(parents=True, exist_ok=True)
        inbox_path = str(inbox_dir / f"{session_id}.jsonl")

        tools._inbox_path = inbox_path
        tools._session_id = session_id

        # Register with local daemon
        daemon_port = config.hub.get("daemon_port", 7700)

        async def _register_session():
            async with httpx.AsyncClient(timeout=5) as http:
                try:
                    await http.post(f"http://localhost:{daemon_port}/api/session/register", json={
                        "session_id": session_id,
                        "project": current_project,
                        "pid": os.getpid(),
                        "inbox_path": inbox_path,
                    })
                except Exception:
                    pass  # Daemon might not be running

        async def _unregister_session():
            async with httpx.AsyncClient(timeout=2) as http:
                try:
                    await http.post(f"http://localhost:{daemon_port}/api/session/unregister", json={
                        "session_id": session_id,
                    })
                except Exception:
                    pass

        # Run registration synchronously at startup
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(_register_session())
        except RuntimeError:
            asyncio.run(_register_session())

        # Unregister on exit
        def _cleanup():
            try:
                asyncio.get_event_loop().run_until_complete(_unregister_session())
            except RuntimeError:
                asyncio.run(_unregister_session())

        atexit.register(_cleanup)
```

Add imports at top of cli.py section:

```python
        from datetime import datetime, timezone
        from pathlib import Path
        import httpx
```

**Step 2: Run full test suite**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add src/cli.py
git commit -m "feat: MCP server registers/unregisters session with daemon"
```

---

### Task 8: check-inbox CLI subcommand and hook script

**Files:**
- Modify: `src/cli.py` (add `check-inbox` subcommand)
- Create: `scripts/check-inbox-hook.sh` (thin wrapper for hooks)

**Step 1: Add check-inbox subcommand**

In `src/cli.py`, add a new subcommand parser after the mcp-server parser:

```python
    # Check inbox (hook)
    inbox_parser = sub.add_parser("check-inbox", help="Check inbox for pending messages")
    inbox_parser.add_argument("--format", choices=["hook", "json"], default="hook")
    inbox_parser.add_argument("--config", default="~/.config/ai-intercom/config.yml")
```

Add handler in the main dispatch:

```python
    elif args.command == "check-inbox":
        import glob
        inbox_dir = os.path.expanduser("~/.config/ai-intercom/inbox")
        if not os.path.isdir(inbox_dir):
            sys.exit(0)

        # Find inbox files with unread messages
        unread_messages = []
        for inbox_file in glob.glob(os.path.join(inbox_dir, "*.jsonl")):
            try:
                with open(inbox_file) as f:
                    lines = f.readlines()
                updated = False
                file_messages = []
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        if not msg.get("read"):
                            unread_messages.append(msg)
                            msg["read"] = True
                            updated = True
                        file_messages.append(msg)
                    except json.JSONDecodeError:
                        file_messages.append(line)

                if updated:
                    with open(inbox_file, "w") as f:
                        for m in file_messages:
                            if isinstance(m, dict):
                                f.write(json.dumps(m) + "\n")
                            else:
                                f.write(m + "\n")
            except Exception:
                pass

        if not unread_messages:
            sys.exit(0)

        if args.format == "json":
            print(json.dumps({"messages": unread_messages, "count": len(unread_messages)}))
        else:
            # Hook format: human-readable for system-reminder injection
            print(f"📨 Messages intercom en attente ({len(unread_messages)}) :\n")
            for msg in unread_messages:
                from_agent = msg.get("from_agent", "unknown")
                thread_id = msg.get("thread_id", "?")
                message = msg.get("message", "")
                ts = msg.get("timestamp", "")
                print(f"[{thread_id}] {from_agent} ({ts}) :")
                print(f'  "{message}"\n')
            print('→ Utilise intercom_reply("thread_id", "ta réponse") pour répondre.')
```

Add `import json` to the top-level imports if not already present.

**Step 2: Create hook wrapper script**

```bash
# scripts/check-inbox-hook.sh
#!/usr/bin/env bash
# Thin wrapper for Claude Code PostToolUse/UserPromptSubmit hooks
# Exits silently if no messages (< 5ms)
exec ai-intercom check-inbox --format hook 2>/dev/null
```

**Step 3: Run tests**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 4: Commit**

```bash
chmod +x scripts/check-inbox-hook.sh
git add src/cli.py scripts/check-inbox-hook.sh
git commit -m "feat: add check-inbox CLI subcommand and hook wrapper"
```

---

### Task 9: Install script updates (hooks + inbox setup)

**Files:**
- Modify: `install.sh` (add hook setup and inbox directory)

**Step 1: Add hook and inbox setup to install.sh**

After the MCP configuration section, add:

```bash
# --- Hook setup for interactive chat ---
echo ""
echo "=== Setting up PostToolUse hooks for chat ==="
INBOX_DIR="${HOME}/.config/ai-intercom/inbox"
mkdir -p "$INBOX_DIR"

SETTINGS_FILE="${HOME}/.claude/settings.local.json"
if [ -f "$SETTINGS_FILE" ]; then
    # Add hooks if not already present
    if ! grep -q "check-inbox" "$SETTINGS_FILE" 2>/dev/null; then
        echo "Adding PostToolUse and UserPromptSubmit hooks to $SETTINGS_FILE"
        python3 -c "
import json, sys
with open('$SETTINGS_FILE') as f:
    settings = json.load(f)
hooks = settings.setdefault('hooks', {})
check_cmd = 'ai-intercom check-inbox --format hook'
for hook_name in ['PostToolUse', 'UserPromptSubmit']:
    existing = hooks.get(hook_name, [])
    if not any(check_cmd in str(h) for h in existing):
        existing.append({'command': check_cmd, 'timeout': 2000})
    hooks[hook_name] = existing
with open('$SETTINGS_FILE', 'w') as f:
    json.dump(settings, f, indent=2)
" 2>/dev/null && echo "Hooks installed." || echo "Could not auto-configure hooks. Add manually."
    else
        echo "Hooks already configured."
    fi
else
    echo "No settings.local.json found. Hooks must be configured manually."
fi
```

**Step 2: Test install script syntax**

Run: `bash -n install.sh`
Expected: no errors

**Step 3: Commit**

```bash
git add install.sh
git commit -m "feat: install.sh sets up PostToolUse hooks and inbox directory"
```

---

### Task 10: Update CHANGELOG, BACKLOG, and version

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `BACKLOG.md`
- Modify: `pyproject.toml:6` (version bump)

**Step 1: Update version to 0.3.0**

In `pyproject.toml` line 6: change `version = "0.1.0"` to `version = "0.3.0"`

**Step 2: Update CHANGELOG**

Add v0.3.0 section to top of CHANGELOG.md:

```markdown
## [0.3.0] - 2026-0X-XX

### Added
- Interactive agent-to-agent chat via `intercom_chat()` and `intercom_reply()`
- `intercom_check_inbox()` tool for manual inbox checking
- Daemon session registration (register/unregister/deliver endpoints)
- PostToolUse and UserPromptSubmit hooks for automatic message delivery
- File-based inbox system (`~/.config/ai-intercom/inbox/`)
- `check-inbox` CLI subcommand for hook integration
- Session status endpoint (`/api/session/<id>/status`)
- Enriched heartbeat with active session info
- Enriched `intercom_list_agents()` showing active sessions
- Chat messages visible in Telegram for human oversight
- `MessageType.CHAT`, `SessionInfo`, `ThreadMessage` models
```

**Step 3: Update BACKLOG**

Mark interactive chat as completed, add follow-up items.

**Step 4: Commit**

```bash
git add CHANGELOG.md BACKLOG.md pyproject.toml
git commit -m "docs: update CHANGELOG and BACKLOG for v0.3.0 interactive chat"
```

---

## Summary

| Task | Component | Files | Est. |
|------|-----------|-------|------|
| 1 | Models | models.py, test_models.py | 5min |
| 2 | Daemon sessions | api.py, test_api.py | 15min |
| 3 | MCP tools | mcp_server.py, test_mcp_server.py | 15min |
| 4 | HubClient | hub_client.py, test_hub_client.py | 10min |
| 5 | Hub routing | hub_api.py, test_hub_api.py | 15min |
| 6 | Heartbeat | main.py, hub_api.py | 10min |
| 7 | Session lifecycle | cli.py | 10min |
| 8 | check-inbox CLI | cli.py, hook script | 10min |
| 9 | Install script | install.sh | 5min |
| 10 | Docs & version | CHANGELOG, BACKLOG, pyproject | 5min |

**Total: ~10 tasks, ~100min estimated**

Each task is independently testable and committable. Tasks 1-4 have no dependencies between them and can be parallelized. Tasks 5-6 depend on 1-4. Tasks 7-9 depend on 2-3. Task 10 is last.
