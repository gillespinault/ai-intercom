# PermissionRequest HTTP Hook — Phase 1 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable remote permission approval for Claude Code sessions via HTTP hooks, eliminating tmux for 80% of interactions.

**Architecture:** Claude Code fires a `PermissionRequest` HTTP hook to the daemon. The daemon forwards to the hub, which broadcasts to the PWA via WebSocket. The human clicks Allow/Deny, the hub resolves the pending request, the daemon returns the decision to Claude Code. Timeout falls back to terminal dialog.

**Tech Stack:** Python 3.12, FastAPI, asyncio, Pydantic, httpx, vanilla JS (PWA)

**Design doc:** `docs/plans/2026-03-06-tmux-free-a2a-attention-design.md`

---

### Task 1: Add Permission Models

**Files:**
- Modify: `src/shared/models.py`
- Test: `tests/test_shared/test_attention_models.py`

**Step 1: Write failing tests**

Add to `tests/test_shared/test_attention_models.py`:

```python
from src.shared.models import PermissionRequest, PermissionDecision


class TestPermissionModels:
    def test_permission_request_from_hook_data(self):
        req = PermissionRequest(
            session_id="abc123",
            tool_name="Bash",
            tool_input={"command": "docker ps"},
        )
        assert req.session_id == "abc123"
        assert req.tool_name == "Bash"
        assert req.tool_input == {"command": "docker ps"}
        assert req.request_id  # auto-generated

    def test_permission_request_with_suggestions(self):
        req = PermissionRequest(
            session_id="abc123",
            tool_name="Bash",
            tool_input={"command": "ls"},
            permission_suggestions=[{"type": "toolAlwaysAllow", "tool": "Bash"}],
        )
        assert len(req.permission_suggestions) == 1

    def test_permission_decision_allow(self):
        d = PermissionDecision(behavior="allow")
        assert d.behavior == "allow"
        assert d.to_hook_response() == {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        }

    def test_permission_decision_deny_with_reason(self):
        d = PermissionDecision(behavior="deny", reason="Not allowed")
        resp = d.to_hook_response()
        assert resp["hookSpecificOutput"]["decision"]["behavior"] == "deny"
        assert resp["hookSpecificOutput"]["decision"]["reason"] == "Not allowed"

    def test_permission_decision_fallback(self):
        """Empty response = fallback to terminal dialog."""
        d = PermissionDecision.fallback()
        assert d.to_hook_response() == {}
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_shared/test_attention_models.py::TestPermissionModels -v`
Expected: FAIL with ImportError (PermissionRequest not defined)

**Step 3: Implement models**

Add to `src/shared/models.py` (after `AttentionEvent`):

```python
class PermissionRequest(BaseModel):
    """A permission request received from Claude Code's PermissionRequest hook."""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    tool_name: str
    tool_input: dict = Field(default_factory=dict)
    permission_suggestions: list[dict] = Field(default_factory=list)
    machine: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class PermissionDecision(BaseModel):
    """A decision for a pending permission request."""
    behavior: str = ""  # "allow" or "deny"
    reason: str = ""

    def to_hook_response(self) -> dict:
        """Format as Claude Code hook response JSON."""
        if not self.behavior:
            return {}  # Empty = fallback to terminal dialog
        decision: dict = {"behavior": self.behavior}
        if self.reason:
            decision["reason"] = self.reason
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": decision,
            }
        }

    @classmethod
    def fallback(cls) -> "PermissionDecision":
        """Create a fallback decision (empty = show terminal dialog)."""
        return cls()
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_shared/test_attention_models.py::TestPermissionModels -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/shared/models.py tests/test_shared/test_attention_models.py
git commit -m "feat: add PermissionRequest and PermissionDecision models"
```

---

### Task 2: Add Pending Approval Store to Hub

**Files:**
- Modify: `src/hub/attention_store.py`
- Test: `tests/test_hub/test_attention_store.py`

**Step 1: Write failing tests**

Add to `tests/test_hub/test_attention_store.py`:

```python
from src.shared.models import PermissionRequest, PermissionDecision


class TestPendingPermissions:
    def test_add_pending_permission(self):
        store = AttentionStore()
        req = PermissionRequest(
            session_id="sess-1",
            tool_name="Bash",
            tool_input={"command": "docker ps"},
            machine="laptop",
        )
        store.add_pending_permission(req)
        assert store.get_pending_permission(req.request_id) is not None

    def test_resolve_permission_allow(self):
        store = AttentionStore()
        req = PermissionRequest(
            session_id="sess-1",
            tool_name="Bash",
            tool_input={"command": "docker ps"},
            machine="laptop",
        )
        store.add_pending_permission(req)
        decision = PermissionDecision(behavior="allow")
        store.resolve_permission(req.request_id, decision)
        assert store.get_pending_permission(req.request_id) is None

    def test_resolve_triggers_event(self):
        store = AttentionStore()
        req = PermissionRequest(
            session_id="sess-1",
            tool_name="Bash",
            tool_input={"command": "docker ps"},
            machine="laptop",
        )
        store.add_pending_permission(req)

        resolved = None
        def on_resolve(request_id, decision):
            nonlocal resolved
            resolved = (request_id, decision)

        store.set_on_permission_resolved(on_resolve)
        decision = PermissionDecision(behavior="deny", reason="nope")
        store.resolve_permission(req.request_id, decision)
        assert resolved is not None
        assert resolved[1].behavior == "deny"

    def test_get_pending_permission_not_found(self):
        store = AttentionStore()
        assert store.get_pending_permission("nonexistent") is None

    def test_list_pending_permissions(self):
        store = AttentionStore()
        req1 = PermissionRequest(session_id="s1", tool_name="Bash", tool_input={}, machine="m1")
        req2 = PermissionRequest(session_id="s2", tool_name="Read", tool_input={}, machine="m1")
        store.add_pending_permission(req1)
        store.add_pending_permission(req2)
        assert len(store.list_pending_permissions()) == 2

    def test_expire_old_permissions(self):
        store = AttentionStore()
        req = PermissionRequest(
            session_id="sess-1",
            tool_name="Bash",
            tool_input={},
            machine="laptop",
            created_at="2020-01-01T00:00:00+00:00",  # Very old
        )
        store.add_pending_permission(req)
        expired = store.expire_permissions(max_age_seconds=60)
        assert len(expired) == 1
        assert store.get_pending_permission(req.request_id) is None
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_attention_store.py::TestPendingPermissions -v`
Expected: FAIL (add_pending_permission not defined)

**Step 3: Implement pending permission methods**

Add to `src/hub/attention_store.py` in `AttentionStore.__init__`:

```python
self._pending_permissions: dict[str, PermissionRequest] = {}
self._on_permission_resolved = None  # callable(request_id, PermissionDecision)
```

Add methods to `AttentionStore`:

```python
# ------------------------------------------------------------------
# Permission approval
# ------------------------------------------------------------------

def set_on_permission_resolved(self, callback) -> None:
    """Set a callback invoked when a permission is resolved."""
    self._on_permission_resolved = callback

def add_pending_permission(self, request: "PermissionRequest") -> None:
    """Store a pending permission request."""
    self._pending_permissions[request.request_id] = request

def get_pending_permission(self, request_id: str) -> "PermissionRequest | None":
    """Look up a pending permission by request_id."""
    return self._pending_permissions.get(request_id)

def list_pending_permissions(self) -> list["PermissionRequest"]:
    """Return all pending permission requests."""
    return list(self._pending_permissions.values())

def resolve_permission(self, request_id: str, decision: "PermissionDecision") -> bool:
    """Resolve a pending permission and notify listeners.

    Returns True if the request existed and was resolved.
    """
    req = self._pending_permissions.pop(request_id, None)
    if req is None:
        return False
    if self._on_permission_resolved:
        self._on_permission_resolved(request_id, decision)
    return True

def expire_permissions(self, max_age_seconds: int = 150) -> list[str]:
    """Remove permissions older than max_age_seconds. Returns expired IDs."""
    now = datetime.now(timezone.utc)
    expired: list[str] = []
    for rid, req in list(self._pending_permissions.items()):
        try:
            created = datetime.fromisoformat(req.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if (now - created).total_seconds() > max_age_seconds:
                expired.append(rid)
                self._pending_permissions.pop(rid, None)
        except (ValueError, TypeError):
            pass
    return expired
```

Add import at top of `attention_store.py`:

```python
from src.shared.models import AttentionEvent, AttentionSession, AttentionState, PermissionRequest, PermissionDecision
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_attention_store.py::TestPendingPermissions -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/hub/attention_store.py tests/test_hub/test_attention_store.py
git commit -m "feat: add pending permission store to AttentionStore"
```

---

### Task 3: Add Hub Permission API Endpoints

**Files:**
- Modify: `src/hub/attention_api.py`
- Test: `tests/test_hub/test_attention_api_permissions.py` (create)

**Step 1: Write failing tests**

Create `tests/test_hub/test_attention_api_permissions.py`:

```python
"""Tests for hub permission API endpoints."""

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient
from fastapi import FastAPI

from src.hub.attention_store import AttentionStore
from src.hub.attention_api import create_attention_router
from src.hub.registry import Registry
from src.shared.models import PermissionRequest


@pytest.fixture
def store():
    return AttentionStore()


@pytest.fixture
def registry():
    return Registry()


@pytest.fixture
def app(store, registry):
    app = FastAPI()
    app.include_router(create_attention_router(store, registry))
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestPermissionEndpoints:
    async def test_post_permission_request(self, client, store):
        resp = await client.post("/api/attention/permission", json={
            "machine": "laptop",
            "session_id": "sess-1",
            "tool_name": "Bash",
            "tool_input": {"command": "docker ps"},
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "request_id" in data
        assert store.get_pending_permission(data["request_id"]) is not None

    async def test_decide_allow(self, client, store):
        # First create a pending request
        req = PermissionRequest(
            session_id="sess-1", tool_name="Bash",
            tool_input={"command": "ls"}, machine="laptop",
        )
        store.add_pending_permission(req)

        resp = await client.post(
            f"/api/attention/permission/{req.request_id}/decide",
            json={"decision": "allow"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resolved"
        assert store.get_pending_permission(req.request_id) is None

    async def test_decide_deny(self, client, store):
        req = PermissionRequest(
            session_id="sess-1", tool_name="Bash",
            tool_input={"command": "rm -rf /"}, machine="laptop",
        )
        store.add_pending_permission(req)

        resp = await client.post(
            f"/api/attention/permission/{req.request_id}/decide",
            json={"decision": "deny", "reason": "Dangerous"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resolved"

    async def test_decide_not_found(self, client):
        resp = await client.post(
            "/api/attention/permission/nonexistent/decide",
            json={"decision": "allow"},
        )
        assert resp.status_code == 404

    async def test_list_pending(self, client, store):
        req = PermissionRequest(
            session_id="sess-1", tool_name="Bash",
            tool_input={}, machine="laptop",
        )
        store.add_pending_permission(req)

        resp = await client.get("/api/attention/permission/pending")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["pending"]) == 1
        assert data["pending"][0]["tool_name"] == "Bash"
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_attention_api_permissions.py -v`
Expected: FAIL (endpoints don't exist)

**Step 3: Implement hub permission endpoints**

Add to `create_attention_router()` in `src/hub/attention_api.py`, after the existing `/stats` endpoints:

```python
from src.shared.models import PermissionRequest, PermissionDecision

@router.post("/permission")
async def receive_permission_request(request: Request):
    """Receive a permission request forwarded by a daemon."""
    data = await request.json()
    req = PermissionRequest(
        session_id=data.get("session_id", ""),
        tool_name=data.get("tool_name", ""),
        tool_input=data.get("tool_input", {}),
        permission_suggestions=data.get("permission_suggestions", []),
        machine=data.get("machine", ""),
    )
    store.add_pending_permission(req)

    # Broadcast to PWA
    await store.broadcast({
        "type": "permission_request",
        "request": req.model_dump(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    return {"status": "pending", "request_id": req.request_id}

@router.post("/permission/{request_id}/decide")
async def decide_permission(request_id: str, request: Request):
    """Resolve a pending permission request with allow/deny."""
    data = await request.json()
    perm = store.get_pending_permission(request_id)
    if not perm:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"error": "not_found"})

    decision = PermissionDecision(
        behavior=data.get("decision", "deny"),
        reason=data.get("reason", ""),
    )
    store.resolve_permission(request_id, decision)

    # Broadcast resolution to PWA
    await store.broadcast({
        "type": "permission_resolved",
        "request_id": request_id,
        "decision": decision.behavior,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    return {"status": "resolved", "request_id": request_id, "decision": decision.behavior}

@router.get("/permission/pending")
async def list_pending_permissions():
    """List all pending permission requests."""
    pending = store.list_pending_permissions()
    return {"pending": [p.model_dump() for p in pending]}
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_attention_api_permissions.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/hub/attention_api.py tests/test_hub/test_attention_api_permissions.py
git commit -m "feat: add hub permission approval API endpoints"
```

---

### Task 4: Add Daemon `/hook/permission` Endpoint

**Files:**
- Modify: `src/daemon/api.py`
- Modify: `src/daemon/hub_client.py`
- Test: `tests/test_daemon/test_api.py`

**Step 1: Write failing tests**

Add to `tests/test_daemon/test_api.py`:

```python
class TestPermissionHook:
    async def test_hook_permission_returns_decision(self, client, app):
        """POST /hook/permission should block until resolved, then return hook response."""
        import asyncio

        # Set up a mock hub_client that resolves immediately
        async def mock_push_permission(req):
            return {"status": "pending", "request_id": req.request_id}

        app.state.hub_client = type("MockHubClient", (), {
            "push_permission_request": mock_push_permission,
        })()

        # Simulate: post permission, then resolve it from another task
        async def resolve_after_delay():
            await asyncio.sleep(0.1)
            # Find the pending future and resolve it
            for rid, fut in list(app.state.pending_permission_futures.items()):
                fut.set_result({"behavior": "allow"})

        asyncio.create_task(resolve_after_delay())

        resp = await client.post("/hook/permission", json={
            "session_id": "abc123",
            "tool_name": "Bash",
            "tool_input": {"command": "docker ps"},
        })
        assert resp.status_code == 200
        data = resp.json()
        # Should return Claude Code hook response format
        assert data.get("hookSpecificOutput", {}).get("decision", {}).get("behavior") == "allow"

    async def test_hook_permission_timeout_returns_empty(self, client, app):
        """If no decision within timeout, return empty (fallback to terminal dialog)."""
        app.state.hub_client = type("MockHubClient", (), {
            "push_permission_request": lambda self, req: {"status": "pending", "request_id": req.request_id},
        })()
        app.state.permission_hook_timeout = 0.2  # 200ms for test

        resp = await client.post("/hook/permission", json={
            "session_id": "abc123",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        })
        assert resp.status_code == 200
        # Empty response = fallback
        assert resp.json() == {}
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_daemon/test_api.py::TestPermissionHook -v`
Expected: FAIL (endpoint not defined)

**Step 3: Implement daemon permission hook endpoint**

Add to `src/daemon/api.py` in `create_app()`, after the attention endpoints:

```python
import asyncio as _asyncio

app.state.pending_permission_futures: dict[str, _asyncio.Future] = {}
app.state.hub_client = None  # Set by daemon main
app.state.permission_hook_timeout = 120  # seconds

@app.post("/hook/permission")
async def hook_permission(request: Request):
    """Receive PermissionRequest hook from Claude Code.

    Blocks until the hub resolves the permission or timeout.
    Returns Claude Code hook response format.
    """
    from src.shared.models import PermissionRequest, PermissionDecision

    data = await request.json()
    req = PermissionRequest(
        session_id=data.get("session_id", ""),
        tool_name=data.get("tool_name", ""),
        tool_input=data.get("tool_input", {}),
        permission_suggestions=data.get("permission_suggestions", []),
        machine=machine_id,
    )

    hub = getattr(app.state, "hub_client", None)
    if not hub:
        logger.warning("Permission hook: no hub_client, falling back to terminal")
        return {}

    # Create a future for this request
    loop = _asyncio.get_event_loop()
    future: _asyncio.Future = loop.create_future()
    app.state.pending_permission_futures[req.request_id] = future

    # Forward to hub
    try:
        await hub.push_permission_request(req)
    except Exception as e:
        logger.error("Failed to forward permission to hub: %s", e)
        app.state.pending_permission_futures.pop(req.request_id, None)
        return {}

    # Wait for resolution or timeout
    timeout = getattr(app.state, "permission_hook_timeout", 120)
    try:
        result = await _asyncio.wait_for(future, timeout=timeout)
        decision = PermissionDecision(
            behavior=result.get("behavior", ""),
            reason=result.get("reason", ""),
        )
        return decision.to_hook_response()
    except _asyncio.TimeoutError:
        logger.info("Permission timeout for %s/%s (request=%s)", req.tool_name, req.session_id, req.request_id)
        return {}
    finally:
        app.state.pending_permission_futures.pop(req.request_id, None)

@app.post("/api/attention/permission/resolve")
async def resolve_permission(request: Request):
    """Called by hub when a permission decision is made.

    Unblocks the waiting /hook/permission handler.
    """
    data = await request.json()
    request_id = data.get("request_id", "")
    behavior = data.get("decision", "")
    reason = data.get("reason", "")

    future = app.state.pending_permission_futures.get(request_id)
    if future and not future.done():
        future.set_result({"behavior": behavior, "reason": reason})
        return {"status": "resolved"}
    return {"status": "not_found"}
```

Add to `src/daemon/hub_client.py`:

```python
async def push_permission_request(self, request: "PermissionRequest") -> dict:
    """Forward a permission request to the hub."""
    return await self._post("/api/attention/permission", {
        "machine": self.machine_id,
        "session_id": request.session_id,
        "tool_name": request.tool_name,
        "tool_input": request.tool_input,
        "permission_suggestions": request.permission_suggestions,
        "request_id": request.request_id,
    })
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_daemon/test_api.py::TestPermissionHook -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest --tb=short -q`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/daemon/api.py src/daemon/hub_client.py tests/test_daemon/test_api.py
git commit -m "feat: add daemon /hook/permission endpoint with async future resolution"
```

---

### Task 5: Wire Hub Decision Back to Daemon

**Files:**
- Modify: `src/hub/attention_api.py` (update `/permission/{request_id}/decide`)
- Modify: `src/hub/attention_store.py`

**Step 1: Write failing test**

Add to `tests/test_hub/test_attention_api_permissions.py`:

```python
from unittest.mock import AsyncMock, patch


class TestPermissionCallbackToDaemon:
    async def test_decide_calls_daemon_resolve(self, client, store, registry):
        """When a permission is decided, hub should POST to daemon to unblock the future."""
        # Register machine with daemon_url
        await registry.register_machine(
            machine_id="laptop",
            display_name="Laptop",
            tailscale_ip="100.1.2.3",
            daemon_url="http://100.1.2.3:7700",
            token="",
        )

        req = PermissionRequest(
            session_id="sess-1", tool_name="Bash",
            tool_input={"command": "ls"}, machine="laptop",
        )
        store.add_pending_permission(req)

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = AsyncMock(json=lambda: {"status": "resolved"})
            resp = await client.post(
                f"/api/attention/permission/{req.request_id}/decide",
                json={"decision": "allow"},
            )
            assert resp.status_code == 200
            # Verify the daemon was called
            mock_post.assert_called_once()
            call_url = mock_post.call_args[0][0]
            assert "/api/attention/permission/resolve" in call_url
```

**Step 2: Run test to verify it fails**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_attention_api_permissions.py::TestPermissionCallbackToDaemon -v`
Expected: FAIL

**Step 3: Update decide endpoint to callback daemon**

Modify the `decide_permission` endpoint in `src/hub/attention_api.py` to POST back to the daemon:

```python
@router.post("/permission/{request_id}/decide")
async def decide_permission(request_id: str, request: Request):
    """Resolve a pending permission request with allow/deny."""
    data = await request.json()
    perm = store.get_pending_permission(request_id)
    if not perm:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"error": "not_found"})

    decision = PermissionDecision(
        behavior=data.get("decision", "deny"),
        reason=data.get("reason", ""),
    )
    store.resolve_permission(request_id, decision)

    # Callback daemon to unblock the waiting hook
    machine = await registry.get_machine(perm.machine)
    if machine and machine.get("daemon_url"):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{machine['daemon_url']}/api/attention/permission/resolve",
                    json={
                        "request_id": request_id,
                        "decision": decision.behavior,
                        "reason": decision.reason,
                    },
                )
        except Exception as e:
            logger.warning("Failed to callback daemon for permission %s: %s", request_id, e)

    # Broadcast resolution to PWA
    await store.broadcast({
        "type": "permission_resolved",
        "request_id": request_id,
        "decision": decision.behavior,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    return {"status": "resolved", "request_id": request_id, "decision": decision.behavior}
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_attention_api_permissions.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/hub/attention_api.py
git commit -m "feat: wire hub permission decision back to daemon via callback"
```

---

### Task 6: Wire Hub Client and Daemon Main

**Files:**
- Modify: `src/daemon/main.py` (wire hub_client to app.state)

**Step 1: Read daemon main.py to understand wiring**

Read `src/daemon/main.py` to find where `app.state` is populated.

**Step 2: Add hub_client wiring**

In `src/daemon/main.py`, after the existing `app.state.launcher = launcher` line, add:

```python
app.state.hub_client = hub_client
```

This ensures `/hook/permission` can forward to the hub.

**Step 3: Run full test suite**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest --tb=short -q`
Expected: All tests pass

**Step 4: Commit**

```bash
git add src/daemon/main.py
git commit -m "feat: wire hub_client to daemon app state for permission forwarding"
```

---

### Task 7: Add WebSocket Permission Actions to Attention API

**Files:**
- Modify: `src/hub/attention_api.py` (WebSocket handler)

**Step 1: Update WebSocket handler**

In the `attention_websocket` handler in `src/hub/attention_api.py`, add handling for `"permission_decide"` action alongside the existing `"respond"` action:

```python
elif action == "permission_decide":
    request_id = msg.get("request_id", "")
    decision_str = msg.get("decision", "deny")
    reason = msg.get("reason", "")

    perm = store.get_pending_permission(request_id)
    if perm:
        decision = PermissionDecision(behavior=decision_str, reason=reason)
        store.resolve_permission(request_id, decision)

        # Callback daemon
        machine = await registry.get_machine(perm.machine)
        if machine and machine.get("daemon_url"):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        f"{machine['daemon_url']}/api/attention/permission/resolve",
                        json={
                            "request_id": request_id,
                            "decision": decision_str,
                            "reason": reason,
                        },
                    )
            except Exception as e:
                logger.warning("WS permission decide failed: %s", e)

        await store.broadcast({
            "type": "permission_resolved",
            "request_id": request_id,
            "decision": decision_str,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
```

**Step 2: Run tests**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest --tb=short -q`
Expected: All pass

**Step 3: Commit**

```bash
git add src/hub/attention_api.py
git commit -m "feat: add WebSocket permission_decide action for PWA"
```

---

### Task 8: PWA Permission UI

**Files:**
- Modify: `pwa/app.js`
- Modify: `pwa/styles.css`
- Modify: `pwa/index.html` (if needed)

This task adds permission request rendering to the PWA. Permission tiles appear as highlighted cards with Allow/Deny buttons.

**Step 1: Add permission state tracking to app.js**

At the top state section of `pwa/app.js`, add:

```javascript
/** Pending permission requests: { request_id: {request_id, session_id, tool_name, tool_input, machine, created_at} } */
var pendingPermissions = {};
```

**Step 2: Handle WebSocket permission events**

In the WebSocket message handler (find the `ws.onmessage` handler), add cases for `permission_request` and `permission_resolved`:

```javascript
case 'permission_request':
  var req = parsed.request;
  if (req && req.request_id) {
    pendingPermissions[req.request_id] = req;
    renderGrid();
    playAlertSound();
    if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
  }
  break;

case 'permission_resolved':
  delete pendingPermissions[parsed.request_id];
  renderGrid();
  break;
```

**Step 3: Render permission tiles in the grid**

In the `renderGrid()` function, before the session tiles, render permission tiles:

```javascript
// Render pending permission tiles first (priority)
Object.values(pendingPermissions).forEach(function(perm) {
  var tile = document.createElement('div');
  tile.className = 'tile tile--permission';
  tile.innerHTML =
    '<div class="tile__header">' +
      '<span class="tile__machine">' + escHtml(perm.machine) + '</span>' +
      '<span class="tile__badge tile__badge--permission">PERMISSION</span>' +
    '</div>' +
    '<div class="tile__tool">' + escHtml(perm.tool_name) + '</div>' +
    '<div class="tile__command">' + escHtml(formatToolInput(perm.tool_name, perm.tool_input)) + '</div>' +
    '<div class="tile__actions">' +
      '<button class="btn btn--allow" data-rid="' + perm.request_id + '">Allow</button>' +
      '<button class="btn btn--deny" data-rid="' + perm.request_id + '">Deny</button>' +
    '</div>';
  grid.appendChild(tile);
});

// Bind permission button events
grid.querySelectorAll('.btn--allow').forEach(function(btn) {
  btn.onclick = function() { sendPermissionDecision(btn.dataset.rid, 'allow'); };
});
grid.querySelectorAll('.btn--deny').forEach(function(btn) {
  btn.onclick = function() { sendPermissionDecision(btn.dataset.rid, 'deny'); };
});
```

**Step 4: Add helper functions**

```javascript
function formatToolInput(toolName, toolInput) {
  if (!toolInput) return '';
  if (toolName === 'Bash' && toolInput.command) return toolInput.command;
  if (toolName === 'Edit' && toolInput.file_path) return toolInput.file_path;
  if (toolName === 'Write' && toolInput.file_path) return toolInput.file_path;
  if (toolName === 'Read' && toolInput.file_path) return toolInput.file_path;
  return JSON.stringify(toolInput).substring(0, 120);
}

function sendPermissionDecision(requestId, decision) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      action: 'permission_decide',
      request_id: requestId,
      decision: decision,
    }));
  }
  // Optimistic removal
  delete pendingPermissions[requestId];
  renderGrid();
}
```

**Step 5: Add CSS for permission tiles**

Add to `pwa/styles.css`:

```css
.tile--permission {
  border-color: #f59e0b;
  background: linear-gradient(135deg, rgba(245, 158, 11, 0.1), rgba(245, 158, 11, 0.05));
  animation: permission-pulse 2s ease-in-out infinite;
}

@keyframes permission-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.3); }
  50% { box-shadow: 0 0 12px 4px rgba(245, 158, 11, 0.15); }
}

.tile__badge--permission {
  background: #f59e0b;
  color: #000;
}

.tile__tool {
  font-size: 1.1em;
  font-weight: 600;
  color: #e2e8f0;
  margin: 4px 0;
}

.tile__command {
  font-family: 'JetBrains Mono', 'Fira Code', monospace;
  font-size: 0.85em;
  color: #94a3b8;
  background: rgba(0, 0, 0, 0.3);
  padding: 6px 8px;
  border-radius: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  margin: 4px 0 8px;
}

.tile__actions {
  display: flex;
  gap: 8px;
}

.btn--allow {
  flex: 1;
  padding: 8px;
  border: none;
  border-radius: 6px;
  background: #22c55e;
  color: #fff;
  font-weight: 600;
  cursor: pointer;
}

.btn--allow:hover { background: #16a34a; }

.btn--deny {
  flex: 1;
  padding: 8px;
  border: none;
  border-radius: 6px;
  background: #ef4444;
  color: #fff;
  font-weight: 600;
  cursor: pointer;
}

.btn--deny:hover { background: #dc2626; }
```

**Step 6: Test manually**

Open the PWA at `https://intercom.robotsinlove.be/attention` and verify that permission tiles would render correctly (functional test after deployment in Task 10).

**Step 7: Commit**

```bash
git add pwa/app.js pwa/styles.css
git commit -m "feat: add permission request tiles with Allow/Deny buttons to PWA"
```

---

### Task 9: Update install.sh Hook Configuration

**Files:**
- Modify: `install.sh`

**Step 1: Read current install.sh**

Read `install.sh` to find where hooks are configured.

**Step 2: Update hook configuration**

In the section that writes `~/.claude/settings.local.json` hooks, update to use the correct format with `matcher` + `hooks` array, and add the `PermissionRequest` HTTP hook:

```json
{
  "hooks": {
    "PermissionRequest": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "http",
            "url": "http://localhost:7331/hook/permission",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
```

**Note:** Preserve existing SessionStart/Stop/Notification/UserPromptSubmit hooks. Only ADD the PermissionRequest hook.

**Step 3: Commit**

```bash
git add install.sh
git commit -m "feat: add PermissionRequest HTTP hook to install.sh"
```

---

### Task 10: Integration Test and Deploy

**Files:**
- No new files

**Step 1: Run full test suite**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest --tb=short -q`
Expected: All tests pass (existing 129+ new tests)

**Step 2: Rebuild and deploy hub**

```bash
cd /home/gilles/serverlab/projects/AI-intercom
docker compose -f docker-compose.hub.yml build --no-cache
docker compose -f docker-compose.hub.yml up -d
```

**Step 3: Reinstall daemon**

```bash
/home/gilles/.local/share/ai-intercom-daemon/venv/bin/pip install -e .
sudo systemctl restart ai-intercom-daemon
```

**Step 4: Configure local hooks**

Add PermissionRequest hook to `~/.claude/settings.local.json` on this machine.

**Step 5: Manual smoke test**

1. Open a new Claude Code session
2. Ask it to run a command that needs permission (e.g. `docker ps`)
3. Verify the permission request appears in the PWA
4. Click Allow in the PWA
5. Verify Claude Code continues without terminal dialog

**Step 6: Final commit**

```bash
git add -A
git commit -m "feat(v0.7.0): PermissionRequest HTTP hook — Phase 1 complete"
```

---

## Summary

| Task | Component | Tests |
|------|-----------|-------|
| 1 | Permission models | 5 |
| 2 | Pending approval store | 6 |
| 3 | Hub permission API | 4+ |
| 4 | Daemon hook endpoint | 2 |
| 5 | Hub → Daemon callback | 1 |
| 6 | Daemon main wiring | - |
| 7 | WebSocket actions | - |
| 8 | PWA permission UI | manual |
| 9 | install.sh hooks | - |
| 10 | Integration & deploy | manual |

**Total estimated new tests: ~18**
**Files modified: ~8 | Files created: ~1**
