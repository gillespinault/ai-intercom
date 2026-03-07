# ccusage Stats in Attention Hub — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Display Claude Code usage statistics (block progress, weekly tokens, per-session context %) in the Attention Hub PWA.

**Architecture:** Daemon collects stats via ccusage CLI + JSONL parsing, pushes to hub via REST, hub broadcasts via WebSocket, PWA renders in header + session tiles.

**Tech Stack:** Python 3.12, FastAPI, ccusage CLI (Node.js), vanilla JS PWA, WebSocket

---

### Task 1: Add Usage Stats Models

**Files:**
- Modify: `src/shared/models.py` (append after line 183)
- Test: `tests/test_shared/test_models_usage.py` (create)

**Step 1: Write the failing test**

Create `tests/test_shared/__init__.py` (empty) and `tests/test_shared/test_models_usage.py`:

```python
"""Tests for usage stats models."""
from src.shared.models import BlockStats, WeeklyStats, SessionContextStats, UsageStatsPayload


def test_block_stats_defaults():
    b = BlockStats(
        start_time="2026-03-04T14:00:00Z",
        end_time="2026-03-04T19:00:00Z",
        elapsed_pct=68.0,
        remaining_minutes=96,
        reset_time="19:00",
        is_active=True,
    )
    assert b.elapsed_pct == 68.0
    assert b.is_active is True


def test_weekly_stats():
    w = WeeklyStats(total_tokens=1_200_000_000, display="1.2B")
    assert w.display == "1.2B"


def test_session_context_stats():
    s = SessionContextStats(context_percent=45.7, context_tokens=91398)
    assert 0 <= s.context_percent <= 100


def test_usage_stats_payload():
    payload = UsageStatsPayload(
        block=BlockStats(
            start_time="2026-03-04T14:00:00Z",
            end_time="2026-03-04T19:00:00Z",
            elapsed_pct=50.0,
            remaining_minutes=150,
            reset_time="19:00",
            is_active=True,
        ),
        weekly=WeeklyStats(total_tokens=500_000_000, display="500M"),
        sessions={"sess-1": SessionContextStats(context_percent=33.0, context_tokens=66000)},
    )
    assert payload.sessions["sess-1"].context_percent == 33.0
```

**Step 2: Run test to verify it fails**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_shared/test_models_usage.py -v`
Expected: FAIL with ImportError (models don't exist yet)

**Step 3: Write minimal implementation**

Append to `src/shared/models.py` after the `AttentionEvent` class:

```python
# ---------------------------------------------------------------------------
# Usage stats models
# ---------------------------------------------------------------------------


class BlockStats(BaseModel):
    """Current billing block progress."""
    start_time: str = ""
    end_time: str = ""
    elapsed_pct: float = 0.0
    remaining_minutes: int = 0
    reset_time: str = ""
    is_active: bool = False


class WeeklyStats(BaseModel):
    """Weekly token usage summary."""
    total_tokens: int = 0
    display: str = "0"


class SessionContextStats(BaseModel):
    """Context window usage for a single session."""
    context_percent: float = 0.0
    context_tokens: int = 0


class UsageStatsPayload(BaseModel):
    """Full usage stats payload pushed from daemon to hub."""
    block: BlockStats = Field(default_factory=BlockStats)
    weekly: WeeklyStats = Field(default_factory=WeeklyStats)
    sessions: dict[str, SessionContextStats] = Field(default_factory=dict)
```

**Step 4: Run test to verify it passes**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_shared/test_models_usage.py -v`
Expected: 4 PASSED

**Step 5: Commit**

```bash
git add src/shared/models.py tests/test_shared/
git commit -m "feat: add usage stats models (BlockStats, WeeklyStats, SessionContextStats)"
```

---

### Task 2: Add UsageCollector to Daemon

**Files:**
- Create: `src/daemon/usage_collector.py`
- Test: `tests/test_daemon/test_usage_collector.py` (create)

**Step 1: Write the failing tests**

Create `tests/test_daemon/test_usage_collector.py`:

```python
"""Tests for the daemon usage collector."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from src.daemon.usage_collector import UsageCollector


def test_format_tokens_compact():
    assert UsageCollector.format_tokens(0) == "0"
    assert UsageCollector.format_tokens(999) == "999"
    assert UsageCollector.format_tokens(1_500) == "1.5K"
    assert UsageCollector.format_tokens(1_200_000) == "1.2M"
    assert UsageCollector.format_tokens(1_200_000_000) == "1.2B"


def test_parse_context_from_jsonl():
    """Parsing last assistant message extracts cache_read_input_tokens."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        # Write a few JSONL lines mimicking Claude Code transcript
        f.write(json.dumps({"type": "user", "message": "hello"}) + "\n")
        f.write(json.dumps({
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 5,
                    "cache_read_input_tokens": 80000,
                    "cache_creation_input_tokens": 200,
                    "output_tokens": 100,
                }
            },
        }) + "\n")
        f.write(json.dumps({
            "type": "assistant",
            "message": {
                "usage": {
                    "input_tokens": 3,
                    "cache_read_input_tokens": 120000,
                    "cache_creation_input_tokens": 500,
                    "output_tokens": 50,
                }
            },
        }) + "\n")
        path = f.name

    try:
        collector = UsageCollector()
        result = collector.get_context_percent(path)
        assert result is not None
        # Last assistant has cache_read=120000 → 120000/200000 = 60%
        assert result.context_tokens == 120000
        assert abs(result.context_percent - 60.0) < 0.1
    finally:
        os.unlink(path)


def test_parse_context_empty_file():
    """Empty JSONL returns None."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        path = f.name
    try:
        collector = UsageCollector()
        assert collector.get_context_percent(path) is None
    finally:
        os.unlink(path)


def test_parse_context_no_usage():
    """JSONL without usage field returns None."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        f.write(json.dumps({"type": "assistant", "message": {"content": "hi"}}) + "\n")
        path = f.name
    try:
        collector = UsageCollector()
        assert collector.get_context_percent(path) is None
    finally:
        os.unlink(path)


def test_parse_block_stats_from_json():
    """Parse active block from ccusage blocks JSON output."""
    blocks_json = {
        "blocks": [
            {
                "id": "2026-03-04T09:00:00.000Z",
                "startTime": "2026-03-04T09:00:00.000Z",
                "endTime": "2026-03-04T14:00:00.000Z",
                "isActive": False,
                "entries": 100,
                "totalTokens": 24000000,
            },
            {
                "id": "2026-03-04T14:00:00.000Z",
                "startTime": "2026-03-04T14:00:00.000Z",
                "endTime": "2026-03-04T19:00:00.000Z",
                "isActive": True,
                "entries": 200,
                "totalTokens": 50000000,
                "projection": {
                    "remainingMinutes": 96,
                    "totalTokens": 300000000,
                    "totalCost": 100.0,
                },
            },
        ],
        "totals": {},
    }
    collector = UsageCollector()
    result = collector.parse_block_stats(blocks_json)
    assert result is not None
    assert result.is_active is True
    assert result.remaining_minutes == 96
    assert result.start_time == "2026-03-04T14:00:00.000Z"
    assert result.end_time == "2026-03-04T19:00:00.000Z"
    assert 0 < result.elapsed_pct <= 100


def test_parse_block_stats_no_active():
    """No active block returns default BlockStats."""
    blocks_json = {
        "blocks": [
            {"isActive": False, "startTime": "x", "endTime": "y"},
        ],
        "totals": {},
    }
    collector = UsageCollector()
    result = collector.parse_block_stats(blocks_json)
    assert result.is_active is False


def test_parse_weekly_stats_from_json():
    """Parse current week from ccusage weekly JSON output."""
    weekly_json = {
        "weekly": [
            {"week": "2026-02-22", "totalTokens": 500000000},
            {"week": "2026-03-01", "totalTokens": 1200000000},
        ],
        "totals": {},
    }
    collector = UsageCollector()
    result = collector.parse_weekly_stats(weekly_json)
    assert result.total_tokens == 1200000000
    assert result.display == "1.2B"
```

**Step 2: Run test to verify it fails**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_daemon/test_usage_collector.py -v`
Expected: FAIL with ModuleNotFoundError

**Step 3: Write implementation**

Create `src/daemon/usage_collector.py`:

```python
"""Collects Claude Code usage statistics from ccusage CLI and JSONL transcripts.

Runs periodically alongside the AttentionMonitor to gather:
- Block progress (time-based %, reset countdown)
- Weekly token usage
- Per-session context window fill percentage
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timezone

from src.shared.models import (
    BlockStats,
    SessionContextStats,
    UsageStatsPayload,
    WeeklyStats,
)

logger = logging.getLogger(__name__)

# How often to run ccusage commands (expensive: spawns node process)
_CCUSAGE_INTERVAL = 60  # seconds

# Context window size for Claude models
_CONTEXT_WINDOW = 200_000


class UsageCollector:
    """Collects usage stats from ccusage CLI and JSONL transcripts."""

    def __init__(self, nvm_dir: str | None = None) -> None:
        self._nvm_dir = nvm_dir or os.path.expanduser("~/.nvm")
        self._last_block: BlockStats = BlockStats()
        self._last_weekly: WeeklyStats = WeeklyStats()
        self._running = False
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Token formatting
    # ------------------------------------------------------------------

    @staticmethod
    def format_tokens(n: int) -> str:
        """Format token count for compact display: 1.2B, 450M, 12K, 999."""
        if n >= 1_000_000_000:
            return f"{n / 1_000_000_000:.1f}B"
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    # ------------------------------------------------------------------
    # JSONL context parsing
    # ------------------------------------------------------------------

    def get_context_percent(self, transcript_path: str) -> SessionContextStats | None:
        """Read the last assistant message's cache_read_input_tokens from a JSONL.

        Returns None if the file is empty or has no assistant messages with usage.
        """
        if not os.path.isfile(transcript_path):
            return None

        last_cache_read = None
        try:
            with open(transcript_path, "rb") as f:
                # Read from end for efficiency on large files
                # Seek to last 64KB (enough for several messages)
                try:
                    f.seek(-65536, 2)
                except OSError:
                    f.seek(0)
                tail = f.read().decode("utf-8", errors="replace")

            for line in tail.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                cache_read = usage.get("cache_read_input_tokens")
                if cache_read is not None:
                    last_cache_read = int(cache_read)
        except OSError as e:
            logger.debug("Cannot read transcript %s: %s", transcript_path, e)
            return None

        if last_cache_read is None:
            return None

        pct = min(100.0, (last_cache_read / _CONTEXT_WINDOW) * 100)
        return SessionContextStats(
            context_percent=round(pct, 1),
            context_tokens=last_cache_read,
        )

    # ------------------------------------------------------------------
    # ccusage CLI execution
    # ------------------------------------------------------------------

    def _run_ccusage(self, *args: str) -> dict | None:
        """Run a ccusage command and return parsed JSON, or None on failure."""
        # Build command with nvm sourcing
        nvm_sh = os.path.join(self._nvm_dir, "nvm.sh")
        if not os.path.isfile(nvm_sh):
            logger.debug("nvm.sh not found at %s", nvm_sh)
            return None

        cmd = (
            f'export NVM_DIR="{self._nvm_dir}" && '
            f'[ -s "{nvm_sh}" ] && . "{nvm_sh}" && '
            f"npx -y ccusage@latest {' '.join(args)}"
        )
        try:
            result = subprocess.run(
                ["bash", "-c", cmd],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout)
        except (subprocess.SubprocessError, json.JSONDecodeError) as e:
            logger.debug("ccusage command failed: %s", e)
        return None

    # ------------------------------------------------------------------
    # Block stats parsing
    # ------------------------------------------------------------------

    def parse_block_stats(self, blocks_json: dict) -> BlockStats:
        """Parse the active block from ccusage blocks --json output."""
        blocks = blocks_json.get("blocks", [])
        active = None
        for b in blocks:
            if b.get("isActive"):
                active = b
                break

        if active is None:
            return BlockStats()

        start_time = active.get("startTime", "")
        end_time = active.get("endTime", "")

        # Calculate elapsed percentage from time
        elapsed_pct = 0.0
        remaining_minutes = 0
        reset_time = ""

        projection = active.get("projection") or {}
        remaining_minutes = int(projection.get("remainingMinutes", 0))

        try:
            start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            total_seconds = (end_dt - start_dt).total_seconds()
            elapsed_seconds = (now - start_dt).total_seconds()
            if total_seconds > 0:
                elapsed_pct = min(100.0, max(0.0, (elapsed_seconds / total_seconds) * 100))
            # Reset time in local HH:MM
            local_end = end_dt.astimezone()
            reset_time = local_end.strftime("%H:%M")
            # Recalculate remaining from time if projection is missing
            if remaining_minutes == 0 and total_seconds > 0:
                remaining_seconds = max(0, (end_dt - now).total_seconds())
                remaining_minutes = int(remaining_seconds / 60)
        except (ValueError, TypeError):
            pass

        return BlockStats(
            start_time=start_time,
            end_time=end_time,
            elapsed_pct=round(elapsed_pct, 1),
            remaining_minutes=remaining_minutes,
            reset_time=reset_time,
            is_active=True,
        )

    # ------------------------------------------------------------------
    # Weekly stats parsing
    # ------------------------------------------------------------------

    def parse_weekly_stats(self, weekly_json: dict) -> WeeklyStats:
        """Parse the latest week from ccusage weekly --json output."""
        weeks = weekly_json.get("weekly", [])
        if not weeks:
            return WeeklyStats()
        # Last entry is the most recent week
        latest = weeks[-1]
        total = int(latest.get("totalTokens", 0))
        return WeeklyStats(total_tokens=total, display=self.format_tokens(total))

    # ------------------------------------------------------------------
    # Collect all stats
    # ------------------------------------------------------------------

    async def collect_ccusage_stats(self) -> None:
        """Run ccusage commands and update cached block + weekly stats."""
        loop = asyncio.get_event_loop()

        blocks_json = await loop.run_in_executor(
            None, self._run_ccusage, "blocks", "--json", "--offline"
        )
        if blocks_json:
            self._last_block = self.parse_block_stats(blocks_json)

        weekly_json = await loop.run_in_executor(
            None, self._run_ccusage, "weekly", "--json", "--offline"
        )
        if weekly_json:
            self._last_weekly = self.parse_weekly_stats(weekly_json)

    def build_payload(
        self, session_contexts: dict[str, SessionContextStats]
    ) -> UsageStatsPayload:
        """Build the full stats payload for pushing to hub."""
        return UsageStatsPayload(
            block=self._last_block,
            weekly=self._last_weekly,
            sessions=session_contexts,
        )

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Periodically collect ccusage stats until stopped."""
        self._running = True
        self._stop_event.clear()
        logger.info("UsageCollector started (interval=%ds)", _CCUSAGE_INTERVAL)
        while self._running:
            try:
                await self.collect_ccusage_stats()
                logger.debug(
                    "ccusage stats: block=%s%% weekly=%s",
                    self._last_block.elapsed_pct,
                    self._last_weekly.display,
                )
            except Exception as e:
                logger.warning("UsageCollector error: %s", e)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=_CCUSAGE_INTERVAL
                )
                break
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        """Signal the run loop to exit."""
        self._running = False
        self._stop_event.set()
```

**Step 4: Run test to verify it passes**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_daemon/test_usage_collector.py -v`
Expected: 7 PASSED

**Step 5: Commit**

```bash
git add src/daemon/usage_collector.py tests/test_daemon/test_usage_collector.py
git commit -m "feat: add UsageCollector for ccusage stats + JSONL context parsing"
```

---

### Task 3: Add Hub Client Method + Hub Stats Endpoint

**Files:**
- Modify: `src/daemon/hub_client.py` (append method)
- Modify: `src/hub/attention_store.py` (add stats storage)
- Modify: `src/hub/attention_api.py` (add /stats endpoint + include in snapshot)
- Test: `tests/test_hub/test_attention_api_stats.py` (create)

**Step 1: Write the failing test**

Create `tests/test_hub/test_attention_api_stats.py`:

```python
"""Tests for usage stats API endpoint and WebSocket broadcast."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.hub.attention_api import create_attention_router
from src.hub.attention_store import AttentionStore
from src.hub.registry import Registry


@pytest.fixture
def app():
    store = AttentionStore(prefs_path="/tmp/test_stats_prefs.json")
    registry = Registry.__new__(Registry)
    registry._machines = {}
    registry._lock = __import__("asyncio").Lock()
    app = FastAPI()
    app.include_router(create_attention_router(store, registry))
    app.state.store = store
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_post_stats(client):
    """POST /api/attention/stats stores and returns ok."""
    stats = {
        "machine_id": "serverlab",
        "stats": {
            "block": {
                "start_time": "2026-03-04T14:00:00Z",
                "end_time": "2026-03-04T19:00:00Z",
                "elapsed_pct": 50.0,
                "remaining_minutes": 150,
                "reset_time": "19:00",
                "is_active": True,
            },
            "weekly": {"total_tokens": 1000000000, "display": "1.0B"},
            "sessions": {
                "sess-1": {"context_percent": 45.0, "context_tokens": 90000}
            },
        },
    }
    resp = client.post("/api/attention/stats", json=stats)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_get_stats(client):
    """GET /api/attention/stats returns stored stats."""
    # First push stats
    stats_payload = {
        "machine_id": "serverlab",
        "stats": {
            "block": {
                "start_time": "2026-03-04T14:00:00Z",
                "end_time": "2026-03-04T19:00:00Z",
                "elapsed_pct": 50.0,
                "remaining_minutes": 150,
                "reset_time": "19:00",
                "is_active": True,
            },
            "weekly": {"total_tokens": 500000000, "display": "500M"},
            "sessions": {},
        },
    }
    client.post("/api/attention/stats", json=stats_payload)

    # Then fetch
    resp = client.get("/api/attention/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["block"]["elapsed_pct"] == 50.0
    assert data["weekly"]["display"] == "500M"


def test_snapshot_includes_stats(client):
    """WebSocket snapshot includes usage_stats."""
    # Push stats first
    stats_payload = {
        "machine_id": "serverlab",
        "stats": {
            "block": {"elapsed_pct": 75.0, "is_active": True,
                      "start_time": "", "end_time": "",
                      "remaining_minutes": 75, "reset_time": "19:00"},
            "weekly": {"total_tokens": 100, "display": "100"},
            "sessions": {},
        },
    }
    client.post("/api/attention/stats", json=stats_payload)

    # Connect WebSocket
    with client.websocket_connect("/api/attention/ws") as ws:
        snapshot = json.loads(ws.receive_text())
        assert snapshot["type"] == "snapshot"
        assert "usage_stats" in snapshot
        assert snapshot["usage_stats"]["block"]["elapsed_pct"] == 75.0
```

**Step 2: Run test to verify it fails**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_attention_api_stats.py -v`
Expected: FAIL (no /stats endpoint)

**Step 3: Implement changes**

**3a.** Add to `src/daemon/hub_client.py` — append after `push_attention_event`:

```python
    async def push_usage_stats(self, stats: dict) -> dict:
        """Push usage stats to the hub."""
        return await self._post("/api/attention/stats", {
            "machine_id": self.machine_id,
            "stats": stats,
        })
```

**3b.** Add to `src/hub/attention_store.py` — add `_usage_stats` field and methods:

In `__init__`, add:
```python
        self._usage_stats: dict = {}
```

Add methods after `get_session`:
```python
    def update_usage_stats(self, stats: dict) -> None:
        """Store the latest usage stats from a daemon."""
        self._usage_stats = stats

    def get_usage_stats(self) -> dict:
        """Return the latest usage stats."""
        return self._usage_stats
```

**3c.** Add to `src/hub/attention_api.py` — add two endpoints after `/prefs`:

```python
    @router.post("/stats")
    async def receive_stats(request: Request):
        """Receive usage stats pushed by a daemon."""
        data = await request.json()
        stats = data.get("stats", {})
        machine_id = data.get("machine_id", "")
        store.update_usage_stats(stats)
        # Broadcast to WebSocket subscribers
        await store.broadcast({
            "type": "usage_stats",
            "stats": stats,
            "machine_id": machine_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return {"status": "ok"}

    @router.get("/stats")
    async def get_stats():
        """Return the latest usage stats."""
        return store.get_usage_stats()
```

**3d.** Modify WebSocket snapshot in `attention_api.py` — add `usage_stats` to initial snapshot:

In the `attention_websocket` function, change the snapshot dict to:
```python
            snapshot = {
                "type": "snapshot",
                "sessions": [s.model_dump() for s in sessions],
                "usage_stats": store.get_usage_stats(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
```

**Step 4: Run test to verify it passes**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_attention_api_stats.py -v`
Expected: 3 PASSED

**Step 5: Run full test suite**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest -x -q`
Expected: All tests pass (no regressions)

**Step 6: Commit**

```bash
git add src/daemon/hub_client.py src/hub/attention_store.py src/hub/attention_api.py tests/test_hub/test_attention_api_stats.py
git commit -m "feat: add /api/attention/stats endpoint and WebSocket broadcast"
```

---

### Task 4: Integrate UsageCollector into Daemon Main Loop

**Files:**
- Modify: `src/daemon/attention_monitor.py` (add context % collection in poll_once)
- Modify: `src/daemon/main.py` (start UsageCollector alongside AttentionMonitor)

**Step 1: Read `src/daemon/main.py`** to understand current startup flow.

**Step 2: Modify `src/daemon/main.py`**

Add import and instantiation of `UsageCollector`. Start it as a background task alongside `AttentionMonitor`. In `poll_once`, after building events, collect context % for active sessions from their `notification_data.transcript_path`.

Key integration point — after the poll cycle in `AttentionMonitor`, collect transcript paths from heartbeats' `notification_data` and call `usage_collector.get_context_percent()` for each, then push the combined payload via `hub_client.push_usage_stats()`.

**Implementation approach:** Add a method to `AttentionMonitor` that collects transcript paths from tracked sessions' notification_data, then call `UsageCollector.get_context_percent()` for each. Push every 3s (same as poll cycle) since context parsing is cheap (just reads last 64KB of file).

Add to `AttentionMonitor.__init__`:
```python
        self._usage_collector: UsageCollector | None = None
```

Add method to `AttentionMonitor`:
```python
    def set_usage_collector(self, collector: UsageCollector) -> None:
        self._usage_collector = collector
```

Modify `poll_once()` — after the push loop, add context collection and stats push:
```python
        # Collect and push usage stats (context % per session)
        if self._hub_client is not None and self._usage_collector is not None:
            session_contexts = {}
            for hb in heartbeats:
                if hb.session_id not in self._tracked:
                    continue
                transcript_path = self._extract_transcript_path(hb)
                if transcript_path:
                    ctx = self._usage_collector.get_context_percent(transcript_path)
                    if ctx:
                        session_contexts[hb.session_id] = ctx
            if session_contexts or self._usage_collector._last_block.is_active:
                payload = self._usage_collector.build_payload(session_contexts)
                try:
                    await self._hub_client.push_usage_stats(payload.model_dump())
                except Exception as exc:
                    logger.debug("Failed to push usage stats: %s", exc)
```

Add helper method:
```python
    @staticmethod
    def _extract_transcript_path(hb: AttentionHeartbeat) -> str | None:
        """Extract transcript_path from heartbeat notification_data."""
        if not hb.notification_data:
            return None
        try:
            data = json.loads(hb.notification_data)
            return data.get("transcript_path")
        except (json.JSONDecodeError, TypeError):
            return None
```

In `src/daemon/main.py`, start UsageCollector:
```python
from src.daemon.usage_collector import UsageCollector

# In startup:
usage_collector = UsageCollector()
monitor.set_usage_collector(usage_collector)
asyncio.create_task(usage_collector.run())
```

**Step 3: Run full test suite**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest -x -q`
Expected: All tests pass

**Step 4: Commit**

```bash
git add src/daemon/attention_monitor.py src/daemon/main.py
git commit -m "feat: integrate UsageCollector into daemon poll loop"
```

---

### Task 5: PWA Header Stats Bar

**Files:**
- Modify: `pwa/index.html` (add stats bar HTML)
- Modify: `pwa/styles.css` (add stats bar CSS)
- Modify: `pwa/app.js` (handle usage_stats events, render header)

**Step 1: Add HTML to `pwa/index.html`**

Replace the `<div class="header-stats" id="header-stats"></div>` with:

```html
      <div class="header-stats" id="header-stats">
        <div class="usage-bar" id="usage-block" title="Block progress">
          <div class="usage-bar-fill" id="usage-block-fill"></div>
          <span class="usage-bar-label" id="usage-block-label">--</span>
        </div>
        <span class="usage-reset" id="usage-reset" title="Reset countdown">--:--</span>
        <span class="usage-weekly" id="usage-weekly" title="Weekly tokens">W: --</span>
      </div>
```

**Step 2: Add CSS to `pwa/styles.css`**

```css
/* --- Usage stats bar --- */
.header-stats {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  font-size: 0.7rem;
  opacity: 0.85;
}

.usage-bar {
  position: relative;
  width: 80px;
  height: 6px;
  background: var(--bg-deep, #111);
  border-radius: 3px;
  overflow: hidden;
}

.usage-bar-fill {
  height: 100%;
  border-radius: 3px;
  background: var(--green, #00d4aa);
  transition: width 0.6s ease, background 0.6s ease;
  width: 0%;
}

.usage-bar-fill.warn { background: var(--yellow, #f0c040); }
.usage-bar-fill.crit { background: var(--red, #e94560); }

.usage-bar-label {
  position: absolute;
  top: -14px;
  right: 0;
  font-size: 0.6rem;
  color: var(--text-dim, #888);
}

.usage-reset {
  color: var(--text-dim, #888);
  white-space: nowrap;
}

.usage-weekly {
  color: var(--text-dim, #888);
  white-space: nowrap;
}
```

**Step 3: Add JS to `pwa/app.js`**

Add a function `updateUsageStats(stats)` and call it when receiving `usage_stats` events or `snapshot`:

```javascript
function updateUsageStats(stats) {
  if (!stats) return;

  // Block bar
  var block = stats.block || {};
  var blockFill = document.getElementById('usage-block-fill');
  var blockLabel = document.getElementById('usage-block-label');
  if (blockFill && block.is_active) {
    var pct = Math.min(100, Math.max(0, block.elapsed_pct || 0));
    blockFill.style.width = pct + '%';
    blockFill.className = 'usage-bar-fill' + (pct > 80 ? ' crit' : pct > 50 ? ' warn' : '');
    if (blockLabel) blockLabel.textContent = Math.round(pct) + '%';
  }

  // Reset countdown
  var resetEl = document.getElementById('usage-reset');
  if (resetEl && block.remaining_minutes != null) {
    var h = Math.floor(block.remaining_minutes / 60);
    var m = block.remaining_minutes % 60;
    var countdown = h > 0 ? h + 'h' + String(m).padStart(2, '0') : m + 'm';
    resetEl.textContent = countdown + ' → ' + (block.reset_time || '--:--');
  }

  // Weekly
  var weeklyEl = document.getElementById('usage-weekly');
  if (weeklyEl && stats.weekly) {
    weeklyEl.textContent = 'W: ' + (stats.weekly.display || '--');
  }
}
```

In the WebSocket message handler, add case for `usage_stats`:
```javascript
if (msg.type === 'usage_stats') {
  updateUsageStats(msg.stats);
}
```

In the snapshot handler, add:
```javascript
if (msg.usage_stats) {
  updateUsageStats(msg.usage_stats);
}
```

**Step 4: Commit**

```bash
git add pwa/index.html pwa/styles.css pwa/app.js
git commit -m "feat: add usage stats header bar to PWA (block progress, reset, weekly)"
```

---

### Task 6: PWA Per-Session Context Bar

**Files:**
- Modify: `pwa/styles.css` (add context bar styles)
- Modify: `pwa/app.js` (render context bar on tiles)

**Step 1: Add CSS for context bar**

```css
/* --- Context bar (per-session tile) --- */
.context-bar {
  margin-top: 0.4rem;
  display: flex;
  align-items: center;
  gap: 0.4rem;
}

.context-bar-track {
  flex: 1;
  height: 4px;
  background: var(--bg-deep, #111);
  border-radius: 2px;
  overflow: hidden;
}

.context-bar-fill {
  height: 100%;
  border-radius: 2px;
  background: var(--green, #00d4aa);
  transition: width 0.6s ease, background 0.6s ease;
  width: 0%;
}

.context-bar-fill.warn { background: var(--yellow, #f0c040); }
.context-bar-fill.crit { background: var(--red, #e94560); }

.context-bar-label {
  font-size: 0.6rem;
  color: var(--text-dim, #888);
  min-width: 2.2em;
  text-align: right;
}
```

**Step 2: Modify tile rendering in `pwa/app.js`**

Store context stats per session in a global map:
```javascript
var sessionContextStats = {};
```

In `updateUsageStats()`, save per-session data:
```javascript
  // Per-session context
  if (stats.sessions) {
    for (var sid in stats.sessions) {
      sessionContextStats[sid] = stats.sessions[sid];
    }
  }
  // Re-render context bars on existing tiles
  updateAllContextBars();
```

Add functions:
```javascript
function renderContextBar(sessionId) {
  var ctx = sessionContextStats[sessionId];
  var pct = ctx ? Math.min(100, Math.max(0, ctx.context_percent || 0)) : 0;
  var cls = pct > 80 ? ' crit' : pct > 50 ? ' warn' : '';
  return '<div class="context-bar">' +
    '<div class="context-bar-track"><div class="context-bar-fill' + cls + '" style="width:' + pct + '%"></div></div>' +
    '<span class="context-bar-label">' + Math.round(pct) + '%</span>' +
    '</div>';
}

function updateAllContextBars() {
  for (var sid in sessionContextStats) {
    var tile = document.querySelector('[data-session-id="' + sid + '"]');
    if (!tile) continue;
    var bar = tile.querySelector('.context-bar');
    if (!bar) {
      // Insert context bar before the actions row (last child)
      var html = renderContextBar(sid);
      var temp = document.createElement('div');
      temp.innerHTML = html;
      var actionsRow = tile.querySelector('.tile-actions');
      if (actionsRow) {
        tile.insertBefore(temp.firstChild, actionsRow);
      } else {
        tile.insertAdjacentHTML('beforeend', html);
      }
    } else {
      // Update existing bar
      var ctx = sessionContextStats[sid];
      var pct = ctx ? Math.min(100, ctx.context_percent || 0) : 0;
      var fill = bar.querySelector('.context-bar-fill');
      var label = bar.querySelector('.context-bar-label');
      if (fill) {
        fill.style.width = pct + '%';
        fill.className = 'context-bar-fill' + (pct > 80 ? ' crit' : pct > 50 ? ' warn' : '');
      }
      if (label) label.textContent = Math.round(pct) + '%';
    }
  }
}
```

In the tile creation function (where new tiles are built), include the context bar in the tile HTML.

**Step 3: Commit**

```bash
git add pwa/styles.css pwa/app.js
git commit -m "feat: add per-session context bar to PWA tiles"
```

---

### Task 7: Integration Test + Deploy

**Files:**
- No new files

**Step 1: Run full test suite**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest -x -q`
Expected: All tests pass

**Step 2: Build and deploy hub**

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

**Step 4: Verify in browser**

- Open Attention Hub PWA
- Check header shows block progress bar + countdown + weekly
- Check session tiles show context bar
- Verify WebSocket receives `usage_stats` events

**Step 5: Final commit**

```bash
git add -A
git commit -m "feat: complete ccusage stats integration in Attention Hub"
```
