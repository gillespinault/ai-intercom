# Hub Intelligent - Push Model Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace pull-based polling (12 hops, 10-20s latency) with push model (6 hops, <1s latency) where daemons push results to Hub.

**Architecture:** Daemons become active: they POST feedback batches every 30s and final results on completion to the Hub. The Hub becomes the single source of truth for mission state. `intercom_status` queries the Hub directly instead of proxying to daemons.

**Tech Stack:** Python 3.12, FastAPI, httpx, asyncio, pytest

**Design doc:** `docs/plans/2026-03-01-hub-intelligent-design.md`

---

### Task 1: Add push_feedback and push_result to HubClient

**Files:**
- Modify: `src/daemon/hub_client.py`
- Test: `tests/test_daemon/test_hub_client.py`

**Step 1: Write failing tests**

Add to `tests/test_daemon/test_hub_client.py`:

```python
@pytest.mark.asyncio
async def test_push_feedback(httpx_mock):
    httpx_mock.add_response(
        url="http://hub:7700/api/missions/m-001/feedback",
        json={"status": "ok"},
    )
    client = HubClient("http://hub:7700", "token", "serverlab")
    result = await client.push_feedback(
        mission_id="m-001",
        feedback=[{"timestamp": "2026-03-01T10:00:00Z", "kind": "tool", "summary": "Reading file"}],
        turn_count=2,
        status="running",
    )
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_push_result(httpx_mock):
    httpx_mock.add_response(
        url="http://hub:7700/api/missions/m-002/result",
        json={"status": "ok"},
    )
    client = HubClient("http://hub:7700", "token", "serverlab")
    result = await client.push_result(
        mission_id="m-002",
        status="completed",
        output="Agent finished successfully",
        feedback=[],
        started_at="2026-03-01T10:00:00Z",
        finished_at="2026-03-01T10:05:00Z",
        turn_count=5,
    )
    assert result["status"] == "ok"
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_daemon/test_hub_client.py -v -k "push"`
Expected: FAIL with `AttributeError: 'HubClient' object has no attribute 'push_feedback'`

**Step 3: Implement push methods**

Add to `src/daemon/hub_client.py` after the existing methods (after line ~106):

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_daemon/test_hub_client.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/daemon/hub_client.py tests/test_daemon/test_hub_client.py
git commit -m "feat: add push_feedback and push_result to HubClient"
```

---

### Task 2: Add receive endpoints on Hub

**Files:**
- Modify: `src/hub/hub_api.py`
- Test: `tests/test_hub/test_hub_api.py`

**Step 1: Write failing tests**

Add to `tests/test_hub/test_hub_api.py`:

```python
async def test_receive_feedback(client, registry):
    """POST /api/missions/{id}/feedback stores feedback in mission_store."""
    await _register_machines(registry)

    # Seed mission_store with initial mission
    app_state = client._transport.app.state
    app_state.mission_store["m-fb-1"] = [{
        "from_agent": "vps/proj", "to_agent": "laptop/proj",
        "type": "ask", "mission_id": "m-fb-1",
    }]

    body = json.dumps({
        "machine_id": "laptop",
        "feedback": [
            {"timestamp": "2026-03-01T10:00:30Z", "kind": "tool", "summary": "Reading config.py"},
        ],
        "turn_count": 2,
        "status": "running",
    }).encode()
    headers = sign_request(body, "laptop", "tok-laptop")

    resp = await client.post("/api/missions/m-fb-1/feedback", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Verify feedback stored in mission_store
    history = app_state.mission_store["m-fb-1"]
    feedback_entry = [m for m in history if m.get("type") == "feedback"]
    assert len(feedback_entry) == 1
    assert feedback_entry[0]["payload"]["turn_count"] == 2


async def test_receive_result(client, registry):
    """POST /api/missions/{id}/result stores final result in mission_store."""
    await _register_machines(registry)

    app_state = client._transport.app.state
    app_state.mission_store["m-res-1"] = [{
        "from_agent": "vps/proj", "to_agent": "laptop/proj",
        "type": "ask", "mission_id": "m-res-1",
    }]

    body = json.dumps({
        "machine_id": "laptop",
        "status": "completed",
        "output": "Done! Here is the result.",
        "feedback": [],
        "started_at": "2026-03-01T10:00:00Z",
        "finished_at": "2026-03-01T10:05:00Z",
        "turn_count": 5,
    }).encode()
    headers = sign_request(body, "laptop", "tok-laptop")

    resp = await client.post("/api/missions/m-res-1/result", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    # Verify result stored
    history = app_state.mission_store["m-res-1"]
    result_entry = [m for m in history if m.get("type") == "result"]
    assert len(result_entry) == 1
    assert result_entry[0]["payload"]["status"] == "completed"
    assert result_entry[0]["payload"]["output"] == "Done! Here is the result."


async def test_receive_result_unknown_mission(client, registry):
    """POST /api/missions/{id}/result for unknown mission returns 404."""
    await _register_machines(registry)

    body = json.dumps({
        "machine_id": "laptop",
        "status": "completed",
        "output": "orphan result",
        "feedback": [],
        "started_at": "2026-03-01T10:00:00Z",
        "finished_at": "2026-03-01T10:05:00Z",
        "turn_count": 1,
    }).encode()
    headers = sign_request(body, "laptop", "tok-laptop")

    resp = await client.post("/api/missions/m-unknown/result", content=body, headers=headers)
    assert resp.status_code == 404


async def test_mission_status_from_store(client, registry):
    """GET /api/missions/{id}/status returns data from mission_store (no proxy)."""
    app_state = client._transport.app.state
    app_state.mission_store["m-st-1"] = [
        {"from_agent": "vps/proj", "to_agent": "laptop/proj", "type": "ask", "mission_id": "m-st-1"},
        {"type": "result", "payload": {
            "status": "completed",
            "output": "All done",
            "feedback": [{"timestamp": "...", "kind": "tool", "summary": "test"}],
            "started_at": "2026-03-01T10:00:00Z",
            "finished_at": "2026-03-01T10:05:00Z",
            "turn_count": 3,
        }},
    ]

    resp = await client.get("/api/missions/m-st-1/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert data["output"] == "All done"
    assert data["turn_count"] == 3
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_hub_api.py -v -k "receive_ or mission_status_from"`
Expected: FAIL (endpoints don't exist yet)

**Step 3: Implement receive endpoints**

Add to `src/hub/hub_api.py` inside `create_hub_api()`, after the existing mission endpoints (~line 590):

```python
    @app.post("/api/missions/{mission_id}/feedback")
    async def receive_feedback(mission_id: str, request: Request):
        """Receive feedback batch from daemon (push model)."""
        body = await request.body()
        data = json.loads(body)

        machine_id = data.get("machine_id", "")
        if machine_id and not await _verify_machine(request, body, machine_id):
            return Response(status_code=401, content="Unauthorized")

        history = app.state.mission_store.get(mission_id)
        if history is None:
            return Response(status_code=404, content="Mission not found")

        history.append({
            "type": "feedback",
            "from_agent": machine_id,
            "mission_id": mission_id,
            "payload": {
                "feedback": data.get("feedback", []),
                "turn_count": data.get("turn_count", 0),
                "status": data.get("status", "running"),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Post to Telegram if available
        if telegram_bot:
            fb_items = data.get("feedback", [])
            if fb_items:
                lines = [f.get("summary", "") for f in fb_items[-5:]]
                elapsed = ""  # Will be calculated from mission start
                text = "\n".join(lines)
                try:
                    first_msg = history[0]
                    to_agent = first_msg.get("to_agent", "")
                    topic_id = getattr(telegram_bot, '_mission_topics', {}).get(mission_id)
                    if topic_id:
                        await telegram_bot.send_message(
                            text=f"⚙️ {text}\n_(turn {data.get('turn_count', '?')})_",
                            message_thread_id=topic_id,
                            parse_mode="Markdown",
                        )
                except Exception:
                    pass  # Non-critical

        return {"status": "ok"}

    @app.post("/api/missions/{mission_id}/result")
    async def receive_result(mission_id: str, request: Request):
        """Receive final mission result from daemon (push model)."""
        body = await request.body()
        data = json.loads(body)

        machine_id = data.get("machine_id", "")
        if machine_id and not await _verify_machine(request, body, machine_id):
            return Response(status_code=401, content="Unauthorized")

        history = app.state.mission_store.get(mission_id)
        if history is None:
            return Response(status_code=404, content="Mission not found")

        result_entry = {
            "type": "result",
            "from_agent": machine_id,
            "mission_id": mission_id,
            "payload": {
                "status": data.get("status", "completed"),
                "output": data.get("output"),
                "feedback": data.get("feedback", []),
                "started_at": data.get("started_at"),
                "finished_at": data.get("finished_at"),
                "turn_count": data.get("turn_count", 0),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        history.append(result_entry)

        # Post to Telegram if available
        if telegram_bot:
            status_emoji = "✅" if data.get("status") == "completed" else "❌"
            output = data.get("output", "")[:3500]
            started = data.get("started_at", "")
            finished = data.get("finished_at", "")
            try:
                first_msg = history[0]
                to_agent = first_msg.get("to_agent", "")
                topic_id = getattr(telegram_bot, '_mission_topics', {}).get(mission_id)
                if topic_id:
                    await telegram_bot.send_message(
                        text=f"{status_emoji} *Termine*\n\n{output}",
                        message_thread_id=topic_id,
                        parse_mode="Markdown",
                    )
            except Exception:
                pass  # Non-critical

        return {"status": "ok"}

    @app.get("/api/missions/{mission_id}/status")
    async def get_mission_status(mission_id: str):
        """Get mission status directly from Hub mission_store (no daemon proxy)."""
        history = app.state.mission_store.get(mission_id)
        if history is None:
            return Response(status_code=404, content="Mission not found")

        # Find the latest result entry
        result_entries = [m for m in history if m.get("type") == "result"]
        if result_entries:
            payload = result_entries[-1]["payload"]
            return {
                "mission_id": mission_id,
                "status": payload.get("status", "completed"),
                "output": payload.get("output"),
                "feedback": payload.get("feedback", []),
                "started_at": payload.get("started_at"),
                "finished_at": payload.get("finished_at"),
                "turn_count": payload.get("turn_count", 0),
            }

        # Find latest feedback entry for running status
        feedback_entries = [m for m in history if m.get("type") == "feedback"]
        if feedback_entries:
            payload = feedback_entries[-1]["payload"]
            # Collect all feedback items from all feedback entries
            all_feedback = []
            for fe in feedback_entries:
                all_feedback.extend(fe.get("payload", {}).get("feedback", []))
            return {
                "mission_id": mission_id,
                "status": payload.get("status", "running"),
                "output": None,
                "feedback": all_feedback,
                "started_at": None,
                "finished_at": None,
                "turn_count": payload.get("turn_count", 0),
            }

        # No result or feedback yet — mission was just launched
        return {
            "mission_id": mission_id,
            "status": "launched",
            "output": None,
            "feedback": [],
            "started_at": None,
            "finished_at": None,
            "turn_count": 0,
        }
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_hub_api.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/hub/hub_api.py tests/test_hub/test_hub_api.py
git commit -m "feat: add Hub receive endpoints for push model (feedback + result)"
```

---

### Task 3: Add feedback pusher to AgentLauncher

**Files:**
- Modify: `src/daemon/agent_launcher.py`
- Test: `tests/test_daemon/test_agent_launcher.py`

**Step 1: Write failing test**

Add to `tests/test_daemon/test_agent_launcher.py`:

```python
async def test_run_agent_pushes_result(tmp_path):
    """After agent completes, _run_agent pushes result to hub_client."""
    mock_hub_client = AsyncMock()
    mock_hub_client.push_result = AsyncMock(return_value={"status": "ok"})
    mock_hub_client.push_feedback = AsyncMock(return_value={"status": "ok"})

    launcher = AgentLauncher(
        default_command="echo",
        default_args=["hello"],
        allowed_paths=[str(tmp_path)],
        max_duration=10,
        hub_client=mock_hub_client,
    )

    mission_id = await launcher.launch_background(
        mission="test",
        context_messages=[],
        mission_id="m-push-1",
        project_path=str(tmp_path),
    )

    # Wait for completion
    await asyncio.sleep(2)

    # Verify push_result was called
    mock_hub_client.push_result.assert_called_once()
    call_kwargs = mock_hub_client.push_result.call_args[1]
    assert call_kwargs["mission_id"] == "m-push-1"
    assert call_kwargs["status"] in ("completed", "failed")
```

**Step 2: Run test to verify it fails**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_daemon/test_agent_launcher.py -v -k "pushes_result"`
Expected: FAIL (hub_client parameter doesn't exist)

**Step 3: Implement hub_client in AgentLauncher**

Modify `src/daemon/agent_launcher.py`:

1. Add `hub_client` parameter to `__init__` (line ~70):
```python
class AgentLauncher:
    def __init__(
        self,
        default_command: str = "claude",
        default_args: list[str] | None = None,
        allowed_paths: list[str] | None = None,
        max_duration: int = 1800,
        hub_client: Any = None,  # NEW
    ):
        self.default_command = default_command
        self.default_args = default_args or ["-p", "--output-format", "stream-json"]
        self.allowed_paths = [str(Path(p).resolve()) for p in (allowed_paths or [])]
        self.max_duration = max_duration
        self.hub_client = hub_client  # NEW
        self._results: dict[str, MissionResult] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._active: dict[str, asyncio.subprocess.Process] = {}
```

2. Add `_feedback_pusher` method and modify `_run_agent` (after line ~338):

```python
    async def _feedback_pusher(self, mission_id: str) -> None:
        """Background task: push feedback batch to Hub every 30s."""
        cursor = 0
        while mission_id in self._results and self._results[mission_id].status == "running":
            await asyncio.sleep(30)
            result = self._results.get(mission_id)
            if not result or result.status != "running":
                break
            new_feedback = result.feedback[cursor:]
            if new_feedback and self.hub_client:
                try:
                    await self.hub_client.push_feedback(
                        mission_id=mission_id,
                        feedback=[{"timestamp": f.timestamp, "kind": f.kind, "summary": f.summary} for f in new_feedback],
                        turn_count=result.turn_count,
                        status="running",
                    )
                    cursor += len(new_feedback)
                except Exception as e:
                    logger.warning("Failed to push feedback for %s: %s", mission_id, e)
```

3. Modify `_run_agent` to push result on completion and start feedback pusher (line ~311):

```python
    async def _run_agent(
        self,
        mission_id: str,
        mission: str,
        context_messages: list[dict],
        project_path: str,
        agent_command: str | None,
    ) -> None:
        """Execute agent in background, store result when done."""
        result = self._results[mission_id]

        # Start feedback pusher if hub_client is available
        fb_task = None
        if self.hub_client:
            fb_task = asyncio.create_task(self._feedback_pusher(mission_id))

        try:
            output = await self.launch_streaming(
                mission=mission,
                context_messages=context_messages,
                mission_id=mission_id,
                project_path=project_path,
                agent_command=agent_command,
            )
            if output.startswith("Error"):
                result.status = "failed"
            else:
                result.status = "completed"
            result.output = output
        except Exception as e:
            result.status = "failed"
            result.output = str(e)
        finally:
            result.finished_at = _now()
            self._tasks.pop(mission_id, None)

            # Cancel feedback pusher
            if fb_task:
                fb_task.cancel()
                try:
                    await fb_task
                except asyncio.CancelledError:
                    pass

            # Push final result to Hub
            if self.hub_client:
                # Collect remaining feedback not yet pushed
                try:
                    await self.hub_client.push_result(
                        mission_id=mission_id,
                        status=result.status,
                        output=result.output,
                        feedback=[{"timestamp": f.timestamp, "kind": f.kind, "summary": f.summary} for f in result.feedback],
                        started_at=result.started_at,
                        finished_at=result.finished_at,
                        turn_count=result.turn_count,
                    )
                except Exception as e:
                    logger.warning("Failed to push result for %s: %s", mission_id, e)
```

**Step 4: Run all launcher tests**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_daemon/test_agent_launcher.py -v`
Expected: ALL PASS (existing tests should pass — hub_client defaults to None)

**Step 5: Commit**

```bash
git add src/daemon/agent_launcher.py tests/test_daemon/test_agent_launcher.py
git commit -m "feat: AgentLauncher pushes feedback and results to Hub"
```

---

### Task 4: Wire hub_client into daemon startup

**Files:**
- Modify: `src/daemon/main.py`
- Modify: `src/daemon/api.py` (pass hub_client to launcher)

**Step 1: Read current daemon/main.py to understand launcher initialization**

Check how `AgentLauncher` is currently instantiated and where to inject `hub_client`.

**Step 2: Modify daemon startup to pass hub_client to AgentLauncher**

In `src/daemon/main.py`, when creating the `AgentLauncher`, pass the existing `hub_client`:

```python
launcher = AgentLauncher(
    default_command=config.agent_launcher.get("default_command", "claude"),
    default_args=config.agent_launcher.get("default_args", ["-p", "--output-format", "stream-json"]),
    allowed_paths=config.agent_launcher.get("allowed_paths", []),
    max_duration=config.agent_launcher.get("max_mission_duration", 1800),
    hub_client=hub_client,  # NEW: pass hub_client for push model
)
```

**Step 3: Run full test suite**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add src/daemon/main.py
git commit -m "feat: wire hub_client into AgentLauncher for push model"
```

---

### Task 5: Remove polling loop and proxy from Hub

**Files:**
- Modify: `src/hub/hub_api.py`

**Step 1: Remove `_track_mission_for_telegram` (lines ~423-568)**

Delete the entire `_track_mission_for_telegram` function (~146 lines).

**Step 2: Remove the `asyncio.create_task(_track_mission_for_telegram(...))` call (lines ~403-421)**

In `route_message()`, remove the block that creates the background tracking task.

**Step 3: Remove `get_daemon_mission_status` endpoint (lines ~673-733)**

Delete the entire `/api/missions/{mission_id}/daemon-status` endpoint (~61 lines).

**Step 4: Remove `_format_elapsed` helper if unused (lines 26-34)**

Check if `_format_elapsed` is used elsewhere. If only used in `_track_mission_for_telegram`, delete it.

**Step 5: Run all tests**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add src/hub/hub_api.py
git commit -m "refactor: remove polling loop and daemon proxy from Hub (-200 LOC)"
```

---

### Task 6: Update intercom_status MCP tool

**Files:**
- Modify: `src/daemon/mcp_server.py`
- Modify: `src/daemon/hub_client.py` (remove get_daemon_mission_status)

**Step 1: Simplify intercom_status**

In `src/daemon/mcp_server.py`, replace the current `intercom_status` (lines ~237-255):

```python
    @mcp.tool()
    async def intercom_status(mission_id: str) -> dict:
        """Get the status of a running mission.

        Returns mission status with output when completed. Poll this after
        intercom_ask to get the agent's response. Status values: "running",
        "completed", "failed", "launched".

        Recommended polling: every 5s for first 30s, then every 10s.
        Timeout after 5 minutes.

        Args:
            mission_id: The mission ID to check.
        """
        return await tools.hub_mission_status(mission_id=mission_id)
```

**Step 2: Add hub_mission_status to IntercomTools**

In `src/daemon/mcp_server.py`, in the `IntercomTools` class, add:

```python
    async def hub_mission_status(self, mission_id: str) -> dict:
        """Get mission status from Hub (push model - Hub has all data)."""
        return await self.hub_client._get(f"/api/missions/{mission_id}/status")
```

**Step 3: Remove daemon_status method from IntercomTools**

Remove the now-unused method:
```python
    async def daemon_status(self, mission_id: str) -> dict:
        return await self.hub_client.get_daemon_mission_status(mission_id=mission_id)
```

**Step 4: Remove get_daemon_mission_status from HubClient**

In `src/daemon/hub_client.py`, remove:
```python
    async def get_daemon_mission_status(self, mission_id: str) -> dict:
        """Get mission status from the daemon running it (via hub proxy)."""
        return await self._get(f"/api/missions/{mission_id}/daemon-status")
```

**Step 5: Run all tests**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add src/daemon/mcp_server.py src/daemon/hub_client.py
git commit -m "refactor: simplify intercom_status to query Hub directly"
```

---

### Task 7: Standalone mode compatibility

**Files:**
- Modify: `src/hub/hub_api.py` (standalone get_mission_status needs local launcher fallback)

**Step 1: Verify standalone mode**

In standalone mode, the Hub has a local `launcher`. The new `/api/missions/{id}/status` endpoint should also check the local launcher as a fallback.

Add to `get_mission_status()` before the mission_store lookup:

```python
    @app.get("/api/missions/{mission_id}/status")
    async def get_mission_status(mission_id: str):
        """Get mission status from Hub mission_store or local launcher (standalone)."""
        # Check local launcher first (standalone mode)
        if app.state.launcher:
            result = app.state.launcher.get_status(mission_id)
            if result:
                return {
                    "mission_id": mission_id,
                    "status": result.status,
                    "output": result.output,
                    "feedback": [
                        {"timestamp": f.timestamp, "kind": f.kind, "summary": f.summary}
                        for f in result.feedback
                    ],
                    "started_at": result.started_at,
                    "finished_at": result.finished_at,
                    "turn_count": result.turn_count,
                }

        # Then check mission_store (push model)
        history = app.state.mission_store.get(mission_id)
        # ... rest of existing code ...
```

**Step 2: Run full test suite**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 3: Commit**

```bash
git add src/hub/hub_api.py
git commit -m "feat: standalone mode fallback for mission status endpoint"
```

---

### Task 8: Update intercom_status docstring for polling guidance

**Files:**
- Modify: `src/daemon/mcp_server.py`

**Step 1: Update intercom_ask docstring**

Update the `intercom_ask` docstring to include polling guidance:

```python
    @mcp.tool()
    async def intercom_ask(...) -> dict:
        """Send a message and wait for a response from another agent.

        Returns immediately with a mission_id. Use intercom_status(mission_id)
        to poll for completion and retrieve the agent's output.

        Polling best practice: call intercom_status() every 5 seconds for the
        first 30 seconds, then every 10 seconds. Timeout after 5 minutes.
        Status will be "completed" or "failed" when done.

        ...
        """
```

**Step 2: Commit**

```bash
git add src/daemon/mcp_server.py
git commit -m "docs: add polling guidance to intercom_ask and intercom_status docstrings"
```

---

### Task 9: Final integration test and cleanup

**Files:**
- Run: Full test suite
- Check: No unused imports or dead code

**Step 1: Run full test suite**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/ -v --tb=short`
Expected: ALL PASS

**Step 2: Check for unused imports**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m py_compile src/hub/hub_api.py && python -m py_compile src/daemon/hub_client.py && python -m py_compile src/daemon/agent_launcher.py && python -m py_compile src/daemon/mcp_server.py && echo "All clean"`

**Step 3: Count LOC change**

Run: `git diff --stat HEAD~8` to verify net LOC reduction.

**Step 4: Final commit if any cleanup needed**

```bash
git add -A
git commit -m "chore: cleanup after push model refactoring"
```

**Step 5: Push to GitHub**

```bash
cd /home/gilles/serverlab && git subtree push --prefix=projects/AI-intercom ai-intercom main
```
