# Notification Filtering + Conversation Memory Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add Telegram notification filtering (controlled from PWA) and SQLite-backed conversation memory for the dispatcher.

**Architecture:** Two independent features. Feature 1 adds a notification_prefs dict to AttentionStore with a JSON persistence file and REST API, checked before sending Telegram alerts. Feature 2 adds a ConversationStore class backed by SQLite, injecting conversation history into the dispatcher prompt.

**Tech Stack:** Python 3.12, FastAPI, SQLite (aiosqlite or sync), python-telegram-bot, existing PWA (vanilla JS)

---

### Task 1: Notification Prefs — AttentionStore

**Files:**
- Modify: `src/hub/attention_store.py`
- Test: `tests/test_hub/test_attention_store.py`

**Step 1: Write failing tests for notification prefs**

Add to `tests/test_hub/test_attention_store.py`:

```python
class TestNotificationPrefs:
    """Tests for Telegram notification preference filtering."""

    def test_default_prefs_all_enabled(self):
        store = AttentionStore()
        prefs = store.get_notification_prefs()
        assert prefs == {"permission": True, "question": True, "text_input": True}

    def test_update_prefs_partial(self):
        store = AttentionStore()
        store.update_notification_prefs({"question": False})
        prefs = store.get_notification_prefs()
        assert prefs["question"] is False
        assert prefs["permission"] is True  # unchanged

    def test_update_prefs_ignores_unknown_keys(self):
        store = AttentionStore()
        store.update_notification_prefs({"unknown_key": True, "permission": False})
        prefs = store.get_notification_prefs()
        assert prefs["permission"] is False
        assert "unknown_key" not in prefs

    def test_should_notify_telegram_respects_prefs(self):
        store = AttentionStore()
        store.update_notification_prefs({"question": False})
        assert store.should_notify_telegram("permission") is True
        assert store.should_notify_telegram("question") is False
        assert store.should_notify_telegram("text_input") is True

    def test_should_notify_telegram_unknown_type_defaults_true(self):
        store = AttentionStore()
        assert store.should_notify_telegram("unknown") is True

    def test_prefs_persistence(self, tmp_path):
        prefs_file = tmp_path / "notification_prefs.json"
        store = AttentionStore(prefs_path=str(prefs_file))
        store.update_notification_prefs({"question": False})

        # New store loads persisted prefs
        store2 = AttentionStore(prefs_path=str(prefs_file))
        assert store2.get_notification_prefs()["question"] is False
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_attention_store.py::TestNotificationPrefs -v`
Expected: FAIL — `get_notification_prefs`, `update_notification_prefs`, `should_notify_telegram` don't exist.

**Step 3: Implement notification prefs in AttentionStore**

Modify `src/hub/attention_store.py`:

- Add `prefs_path` parameter to `__init__` (default `"data/notification_prefs.json"`)
- Add `_notification_prefs: dict[str, bool]` initialized with defaults `{"permission": True, "question": True, "text_input": True}`
- Load from JSON file in `__init__` if file exists
- Add `get_notification_prefs() -> dict[str, bool]`
- Add `update_notification_prefs(updates: dict) -> dict[str, bool]` — merges only known keys, saves to JSON file, returns updated prefs
- Add `should_notify_telegram(prompt_type: str) -> bool` — returns `self._notification_prefs.get(prompt_type, True)`

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_attention_store.py::TestNotificationPrefs -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/hub/attention_store.py tests/test_hub/test_attention_store.py
git commit -m "feat: add notification prefs to AttentionStore with JSON persistence"
```

---

### Task 2: Notification Prefs — Check Before Telegram Send

**Files:**
- Modify: `src/hub/attention_store.py` (handle_event callback logic)
- Modify: `src/hub/telegram_bot.py:338` (send_attention_notification)
- Test: `tests/test_hub/test_attention_store.py`

**Step 1: Write failing test for filtered callback**

Add to `tests/test_hub/test_attention_store.py`:

```python
class TestNotificationPrefsFiltering:
    """Tests that notification prefs filter Telegram callbacks."""

    @pytest.mark.asyncio
    async def test_waiting_callback_skipped_when_type_disabled(self):
        """If question prefs is False, callback is NOT called for question prompts."""
        store = AttentionStore()
        callback = AsyncMock()
        store.set_on_waiting_callback(callback)
        store.update_notification_prefs({"question": False})

        session_data = _make_session(state=AttentionState.WAITING).model_dump()
        session_data["prompt"] = {"type": "question", "raw_text": "Choose one"}

        store.handle_event("laptop", {"type": "state_changed", "session": session_data})
        await asyncio.sleep(0)

        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_waiting_callback_called_when_type_enabled(self):
        """If permission prefs is True, callback IS called for permission prompts."""
        store = AttentionStore()
        callback = AsyncMock()
        store.set_on_waiting_callback(callback)
        # permission is True by default

        session_data = _make_session(state=AttentionState.WAITING).model_dump()
        session_data["prompt"] = {"type": "permission", "raw_text": "Allow Bash?", "tool": "Bash"}

        store.handle_event("laptop", {"type": "state_changed", "session": session_data})
        await asyncio.sleep(0)

        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_waiting_callback_called_when_no_prompt(self):
        """Sessions in WAITING without a prompt always trigger callback."""
        store = AttentionStore()
        callback = AsyncMock()
        store.set_on_waiting_callback(callback)

        store.handle_event("laptop", {
            "type": "new_session",
            "session": _make_session(state=AttentionState.WAITING).model_dump(),
        })
        await asyncio.sleep(0)

        callback.assert_called_once()
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_attention_store.py::TestNotificationPrefsFiltering -v`
Expected: FAIL — prefs not checked in `handle_event`

**Step 3: Modify handle_event to check prefs before callback**

In `src/hub/attention_store.py`, modify the WAITING notification block (lines 73-78):

```python
if session.state == AttentionState.WAITING:
    if session.session_id not in self._notified_waiting:
        self._notified_waiting.add(session.session_id)
        if self._on_waiting_callback:
            # Check notification prefs before Telegram callback
            prompt_type = session.prompt.type if session.prompt else None
            if prompt_type is None or self.should_notify_telegram(prompt_type):
                asyncio.create_task(self._on_waiting_callback(session))
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_attention_store.py -v`
Expected: ALL PASS (new + existing)

**Step 5: Commit**

```bash
git add src/hub/attention_store.py tests/test_hub/test_attention_store.py
git commit -m "feat: filter Telegram attention alerts based on notification prefs"
```

---

### Task 3: Notification Prefs — REST API Endpoints

**Files:**
- Modify: `src/hub/attention_api.py`
- Test: `tests/test_hub/test_attention_api_prefs.py` (new)

**Step 1: Write failing tests for API endpoints**

Create `tests/test_hub/test_attention_api_prefs.py`:

```python
"""Tests for notification prefs REST API endpoints."""

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

from src.hub.attention_api import create_attention_router
from src.hub.attention_store import AttentionStore
from src.hub.registry import Registry


@pytest.fixture
def app(tmp_path):
    store = AttentionStore(prefs_path=str(tmp_path / "prefs.json"))
    # Use a mock registry (not async-initialized)
    from unittest.mock import AsyncMock, MagicMock
    registry = MagicMock(spec=Registry)
    router = create_attention_router(store, registry)
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestPrefsAPI:
    def test_get_prefs_returns_defaults(self, client):
        resp = client.get("/api/attention/prefs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["permission"] is True
        assert data["question"] is True
        assert data["text_input"] is True

    def test_patch_prefs_updates(self, client):
        resp = client.patch("/api/attention/prefs", json={"question": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["question"] is False
        assert data["permission"] is True

    def test_patch_prefs_persists(self, client):
        client.patch("/api/attention/prefs", json={"text_input": False})
        resp = client.get("/api/attention/prefs")
        assert resp.json()["text_input"] is False

    def test_patch_prefs_ignores_unknown(self, client):
        resp = client.patch("/api/attention/prefs", json={"foo": True})
        assert resp.status_code == 200
        assert "foo" not in resp.json()
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_attention_api_prefs.py -v`
Expected: FAIL — endpoints don't exist

**Step 3: Add GET/PATCH endpoints to attention_api.py**

In `src/hub/attention_api.py`, inside `create_attention_router()`, add:

```python
@router.get("/prefs")
async def get_notification_prefs():
    """Return current Telegram notification preferences."""
    return store.get_notification_prefs()

@router.patch("/prefs")
async def update_notification_prefs(request: Request):
    """Update Telegram notification preferences (partial merge)."""
    updates = await request.json()
    updated = store.update_notification_prefs(updates)
    # Broadcast to all PWA clients so they stay in sync
    await store.broadcast({"type": "prefs_updated", "prefs": updated})
    return updated
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_attention_api_prefs.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/hub/attention_api.py tests/test_hub/test_attention_api_prefs.py
git commit -m "feat: add GET/PATCH /api/attention/prefs endpoints"
```

---

### Task 4: Notification Prefs — PWA Wiring

**Files:**
- Modify: `pwa/app.js`

**Step 1: Add hub prefs sync to app.js**

The PWA already has toggle elements (`pref-permission`, `pref-question`, `pref-text-input`) and localStorage persistence. Add:

1. **On connect** (after WebSocket snapshot): fetch `GET /api/attention/prefs` and sync toggles
2. **On toggle change**: in addition to localStorage, also `PATCH /api/attention/prefs` to hub
3. **On `prefs_updated` WebSocket event**: sync toggles from hub (for multi-client sync)

In the `init()` function, after `connectWS()`:
```javascript
// Fetch hub notification prefs and sync toggles
fetch('/api/attention/prefs')
  .then(r => r.json())
  .then(hubPrefs => {
    ['permission', 'question', 'text_input'].forEach(type => {
      var key = 'pref-' + type.replace('_', '-');
      var el = document.getElementById(key);
      if (el && hubPrefs[type] !== undefined) {
        el.checked = hubPrefs[type];
        prefs[key] = hubPrefs[type];
        savePrefs();
      }
    });
  })
  .catch(() => {}); // offline = use localStorage
```

In the toggle change handler, add:
```javascript
// Sync prompt type toggles to hub
if (['pref-permission', 'pref-question', 'pref-text-input'].includes(key)) {
  var typeMap = {'pref-permission': 'permission', 'pref-question': 'question', 'pref-text-input': 'text_input'};
  var body = {};
  body[typeMap[key]] = val;
  fetch('/api/attention/prefs', {method: 'PATCH', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).catch(() => {});
}
```

In the WebSocket message handler, add case for `prefs_updated`:
```javascript
if (msg.type === 'prefs_updated' && msg.prefs) {
  ['permission', 'question', 'text_input'].forEach(type => {
    var key = 'pref-' + type.replace('_', '-');
    var el = document.getElementById(key);
    if (el && msg.prefs[type] !== undefined) {
      el.checked = msg.prefs[type];
      prefs[key] = msg.prefs[type];
    }
  });
  savePrefs();
}
```

**Step 2: Test manually**

1. Open PWA at `https://attention.robotsinlove.be`
2. Open settings panel (gear icon)
3. Toggle off "Question prompts"
4. Verify: `curl https://attention.robotsinlove.be/api/attention/prefs` returns `{"permission": true, "question": false, "text_input": true}`
5. Trigger a Claude Code question prompt → verify NO Telegram notification but PWA still shows session

**Step 3: Commit**

```bash
git add pwa/app.js
git commit -m "feat: wire PWA notification toggles to hub API for Telegram filtering"
```

---

### Task 5: Conversation Store — SQLite Module

**Files:**
- Create: `src/hub/conversation_store.py`
- Test: `tests/test_hub/test_conversation_store.py` (new)

**Step 1: Write failing tests**

Create `tests/test_hub/test_conversation_store.py`:

```python
"""Tests for the dispatcher conversation memory store."""

import time
import pytest

from src.hub.conversation_store import ConversationStore


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "conversations.db")
    s = ConversationStore(db_path=db_path)
    s.init()
    return s


class TestAddAndGetHistory:
    def test_add_and_retrieve(self, store):
        store.add_message(user_id=123, role="user", content="Salut")
        store.add_message(user_id=123, role="assistant", content="Bonjour!")

        history = store.get_history(user_id=123, limit=10)
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "Salut"
        assert history[1]["role"] == "assistant"
        assert history[1]["content"] == "Bonjour!"

    def test_history_ordered_by_timestamp(self, store):
        store.add_message(user_id=123, role="user", content="First")
        store.add_message(user_id=123, role="assistant", content="Second")
        store.add_message(user_id=123, role="user", content="Third")

        history = store.get_history(user_id=123, limit=10)
        assert [h["content"] for h in history] == ["First", "Second", "Third"]

    def test_history_respects_limit(self, store):
        for i in range(20):
            store.add_message(user_id=123, role="user", content=f"msg-{i}")

        history = store.get_history(user_id=123, limit=5)
        assert len(history) == 5
        # Should return the LAST 5 messages
        assert history[0]["content"] == "msg-15"

    def test_history_per_user(self, store):
        store.add_message(user_id=100, role="user", content="User 100")
        store.add_message(user_id=200, role="user", content="User 200")

        h100 = store.get_history(user_id=100, limit=10)
        h200 = store.get_history(user_id=200, limit=10)
        assert len(h100) == 1
        assert len(h200) == 1
        assert h100[0]["content"] == "User 100"


class TestCleanup:
    def test_cleanup_removes_old_messages(self, store):
        # Insert a message with old timestamp
        import sqlite3
        conn = sqlite3.connect(store.db_path)
        old_ts = time.time() - 3600 * 72  # 72 hours ago
        conn.execute(
            "INSERT INTO conversations (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (123, "user", "old", old_ts),
        )
        conn.commit()
        conn.close()

        store.add_message(user_id=123, role="user", content="recent")

        removed = store.cleanup(max_age_hours=48)
        assert removed == 1

        history = store.get_history(user_id=123, limit=10)
        assert len(history) == 1
        assert history[0]["content"] == "recent"


class TestSearch:
    def test_search_finds_matching_messages(self, store):
        store.add_message(user_id=123, role="user", content="deploy the backup script")
        store.add_message(user_id=123, role="assistant", content="Done, backup deployed")
        store.add_message(user_id=123, role="user", content="check disk space")

        results = store.search(user_id=123, query="backup", limit=5)
        assert len(results) == 2
        assert all("backup" in r["content"].lower() for r in results)

    def test_search_empty_returns_empty(self, store):
        results = store.search(user_id=123, query="nonexistent", limit=5)
        assert results == []


class TestBuildPromptContext:
    def test_builds_formatted_history(self, store):
        store.add_message(user_id=123, role="user", content="What services are running?")
        store.add_message(user_id=123, role="assistant", content="PostgreSQL, n8n, Neo4j are running.")

        context = store.build_prompt_context(user_id=123, limit=10, max_content_length=500)
        assert "User:" in context
        assert "Assistant:" in context
        assert "PostgreSQL" in context

    def test_truncates_long_content(self, store):
        long_msg = "x" * 1000
        store.add_message(user_id=123, role="user", content=long_msg)

        context = store.build_prompt_context(user_id=123, limit=10, max_content_length=100)
        # Each message should be truncated to 100 chars
        lines = [l for l in context.split("\n") if "User:" in l]
        assert len(lines) == 1
        # Content portion should be <= 100 chars + the prefix
        assert len(lines[0]) < 150

    def test_empty_history_returns_empty_string(self, store):
        context = store.build_prompt_context(user_id=123, limit=10, max_content_length=500)
        assert context == ""
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_conversation_store.py -v`
Expected: FAIL — module doesn't exist

**Step 3: Implement ConversationStore**

Create `src/hub/conversation_store.py`:

```python
"""SQLite-backed conversation memory for the Telegram dispatcher."""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime

logger = logging.getLogger(__name__)


class ConversationStore:
    """Stores dispatcher conversation history per Telegram user.

    Provides methods to add messages, retrieve recent history,
    search past messages, and build formatted prompt context.
    """

    def __init__(self, db_path: str = "data/conversations.db") -> None:
        self.db_path = db_path

    def init(self) -> None:
        """Create the database schema if it doesn't exist."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                timestamp REAL NOT NULL,
                mission_id TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_conv_user_ts
            ON conversations(user_id, timestamp DESC)
        """)
        conn.commit()
        conn.close()
        logger.info("ConversationStore initialized at %s", self.db_path)

    def add_message(
        self,
        user_id: int,
        role: str,
        content: str,
        mission_id: str | None = None,
    ) -> None:
        """Store a conversation message."""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO conversations (user_id, role, content, timestamp, mission_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, role, content, time.time(), mission_id),
        )
        conn.commit()
        conn.close()

    def get_history(self, user_id: int, limit: int = 10) -> list[dict]:
        """Return the last N messages for a user, oldest first."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT role, content, timestamp FROM conversations "
            "WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        conn.close()
        # Reverse to get chronological order
        return [dict(r) for r in reversed(rows)]

    def search(self, user_id: int, query: str, limit: int = 5) -> list[dict]:
        """Search conversation history for messages containing query."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT role, content, timestamp FROM conversations "
            "WHERE user_id = ? AND content LIKE ? ORDER BY timestamp DESC LIMIT ?",
            (user_id, f"%{query}%", limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def cleanup(self, max_age_hours: int = 48) -> int:
        """Remove messages older than max_age_hours. Returns count removed."""
        cutoff = time.time() - (max_age_hours * 3600)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "DELETE FROM conversations WHERE timestamp < ?", (cutoff,)
        )
        removed = cursor.rowcount
        conn.commit()
        conn.close()
        if removed:
            logger.info("Cleaned up %d old conversation messages", removed)
        return removed

    def build_prompt_context(
        self,
        user_id: int,
        limit: int = 10,
        max_content_length: int = 500,
    ) -> str:
        """Build a formatted conversation history string for prompt injection."""
        history = self.get_history(user_id, limit)
        if not history:
            return ""

        lines: list[str] = []
        for msg in history:
            ts = datetime.fromtimestamp(msg["timestamp"]).strftime("%H:%M")
            role = "User" if msg["role"] == "user" else "Assistant"
            content = msg["content"]
            if len(content) > max_content_length:
                content = content[:max_content_length] + "..."
            lines.append(f"[{ts}] {role}: {content}")

        return "\n".join(lines)
```

**Step 4: Run tests to verify they pass**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_conversation_store.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/hub/conversation_store.py tests/test_hub/test_conversation_store.py
git commit -m "feat: add ConversationStore with SQLite persistence for dispatcher memory"
```

---

### Task 6: Conversation Memory — Wire Into Dispatcher

**Files:**
- Modify: `src/hub/main.py` (on_dispatch function)
- Modify: `config/config.yml` (add dispatcher.memory section)
- Modify: `src/shared/config.py` (no changes needed — dispatcher is already a dict)

**Step 1: Add dispatcher.memory config to config.yml**

Add to `config/config.yml` under the `dispatcher:` section:

```yaml
dispatcher:
  enabled: true
  target: "serverlab/home"
  memory:
    enabled: true
    max_messages: 10
    max_content_length: 500
    ttl_hours: 48
    db_path: "data/conversations.db"
  system_prompt: |
    ...existing prompt...
```

**Step 2: Wire ConversationStore into on_dispatch**

In `src/hub/main.py`, in `run_hub()`:

1. After `Path("data").mkdir(...)`, initialize the ConversationStore:
```python
# Conversation memory for dispatcher
from src.hub.conversation_store import ConversationStore
conv_store = None
memory_cfg = config.dispatcher.get("memory", {})
if memory_cfg.get("enabled", False):
    conv_store = ConversationStore(db_path=memory_cfg.get("db_path", "data/conversations.db"))
    conv_store.init()
    conv_store.cleanup(max_age_hours=memory_cfg.get("ttl_hours", 48))
    logger.info("Dispatcher conversation memory enabled")
```

2. In `on_dispatch()`, before building the mission, inject history:
```python
# Build mission with conversation history
if conv_store:
    user_id = update.effective_user.id
    # Store user message
    conv_store.add_message(user_id=user_id, role="user", content=text)
    # Build history context
    history_context = conv_store.build_prompt_context(
        user_id=user_id,
        limit=memory_cfg.get("max_messages", 10),
        max_content_length=memory_cfg.get("max_content_length", 500),
    )
    if history_context:
        mission = f"{system_prompt}\n\n## Conversation history\n{history_context}\n\nUser message:\n{text}"
    else:
        mission = f"{system_prompt}\n\nUser message:\n{text}" if system_prompt else text
else:
    mission = f"{system_prompt}\n\nUser message:\n{text}" if system_prompt else text
```

3. After receiving the response (after the final output is extracted), store the assistant response:
```python
# Store assistant response in conversation memory
if conv_store and output and status == "completed":
    conv_store.add_message(
        user_id=update.effective_user.id,
        role="assistant",
        content=output[:2000],  # Truncate very long responses
        mission_id=resp_mission_id,
    )
```

**Step 3: Test manually**

1. Rebuild hub: `docker compose -f docker-compose.hub.yml build --no-cache && docker compose -f docker-compose.hub.yml up -d`
2. Send "Quels services tournent?" via Telegram
3. Wait for response
4. Send "Et sur limn?" via Telegram
5. Verify the dispatcher's response shows awareness of the previous question (e.g., references services context)
6. Verify `data/conversations.db` contains the messages

**Step 4: Commit**

```bash
git add src/hub/main.py config/config.yml
git commit -m "feat: wire conversation memory into dispatcher with configurable history window"
```

---

### Task 7: Conversation History — REST API Endpoint

**Files:**
- Modify: `src/hub/hub_api.py`
- Test: `tests/test_hub/test_conversation_api.py` (new)

**Step 1: Write failing test**

Create `tests/test_hub/test_conversation_api.py`:

```python
"""Tests for dispatcher conversation history API endpoint."""

import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from fastapi import FastAPI

from src.hub.conversation_store import ConversationStore


@pytest.fixture
def store(tmp_path):
    s = ConversationStore(db_path=str(tmp_path / "conv.db"))
    s.init()
    s.add_message(user_id=123, role="user", content="deploy backup")
    s.add_message(user_id=123, role="assistant", content="Backup deployed successfully")
    s.add_message(user_id=123, role="user", content="check disk space")
    return s


@pytest.fixture
def app(store):
    from src.hub.hub_api import create_hub_api
    from src.shared.config import IntercomConfig
    # We'll add the endpoint directly for testing
    app = FastAPI()

    @app.get("/api/dispatcher/history")
    def get_history(user_id: int, query: str = "", limit: int = 5):
        if query:
            return {"messages": store.search(user_id, query, limit)}
        return {"messages": store.get_history(user_id, limit)}

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestHistoryEndpoint:
    def test_get_history(self, client):
        resp = client.get("/api/dispatcher/history", params={"user_id": 123, "limit": 10})
        assert resp.status_code == 200
        assert len(resp.json()["messages"]) == 3

    def test_search_history(self, client):
        resp = client.get("/api/dispatcher/history", params={"user_id": 123, "query": "backup"})
        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert len(msgs) == 2
        assert all("backup" in m["content"].lower() for m in msgs)
```

**Step 2: Run tests to verify they fail**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_hub/test_conversation_api.py -v`
Expected: Tests define inline endpoint so they should pass as a standalone test. The real work is wiring into hub_api.py.

**Step 3: Add endpoint to hub_api.py**

In `src/hub/hub_api.py`, add a dispatcher history endpoint. The `create_hub_api` function receives the config and can access `conv_store` via `app.state`:

```python
@app.get("/api/dispatcher/history")
async def dispatcher_history(user_id: int, query: str = "", limit: int = 5):
    """Search or retrieve dispatcher conversation history."""
    conv_store = getattr(app.state, "conversation_store", None)
    if not conv_store:
        return {"messages": [], "error": "Conversation memory not enabled"}
    if query:
        return {"messages": conv_store.search(user_id, query, limit)}
    return {"messages": conv_store.get_history(user_id, limit)}
```

In `src/hub/main.py`, after creating `hub_api`, set `conv_store` on app state:
```python
if conv_store:
    hub_api.state.conversation_store = conv_store
```

**Step 4: Run all tests**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/ -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/hub/hub_api.py src/hub/main.py tests/test_hub/test_conversation_api.py
git commit -m "feat: add GET /api/dispatcher/history endpoint for conversation search"
```

---

### Task 8: Docker Build + Deploy + Integration Test

**Files:**
- Modify: `docker-compose.hub.yml` (mount data/ for SQLite persistence)

**Step 1: Verify data/ volume is mounted**

Check `docker-compose.hub.yml` — the `data/` directory should already be mounted. If not, add:
```yaml
volumes:
  - ./data:/app/data
```

**Step 2: Rebuild and deploy**

```bash
cd /home/gilles/serverlab/projects/AI-intercom
docker compose -f docker-compose.hub.yml build --no-cache
docker compose -f docker-compose.hub.yml up -d
```

**Step 3: Integration test — notification filtering**

1. Open PWA, toggle off "Question prompts" in settings
2. Trigger a question prompt in Claude Code (e.g., AskUserQuestion)
3. Verify: NO Telegram notification, but PWA shows the session
4. Toggle "Question prompts" back on
5. Trigger another question prompt
6. Verify: Telegram notification IS sent

**Step 4: Integration test — conversation memory**

1. Send via Telegram: "Quels services sont actifs?"
2. Wait for response
3. Send: "Et sur limn?"
4. Verify response references the previous context
5. Check API: `curl https://attention.robotsinlove.be/api/dispatcher/history?user_id=7700374014&limit=10`

**Step 5: Final commit and update docs**

```bash
git add docker-compose.hub.yml
git commit -m "feat(v0.6.0): notification filtering + conversation memory deployed"
```

---

### Task 9: Update ROADMAP.md and BACKLOG.md

**Files:**
- Modify: `ROADMAP.md`
- Modify: `BACKLOG.md`
- Modify: `CHANGELOG.md`

**Step 1: Update ROADMAP.md**

- Update "Current" version to v0.6.0
- Add notification filtering and conversation memory to the Done table
- Remove them from "Next Steps"

**Step 2: Update BACKLOG.md**

- Mark items 3 (Continuite conversationnelle) as done with version reference
- Add notification filtering to the "Fait" section

**Step 3: Update CHANGELOG.md**

Add `## [0.6.0] - 2026-03-04` section with:
- Added: Telegram notification filtering with per-prompt-type toggles
- Added: PWA settings sync for notification preferences
- Added: SQLite-backed conversation memory for dispatcher
- Added: `GET /api/dispatcher/history` endpoint

**Step 4: Commit**

```bash
git add ROADMAP.md BACKLOG.md CHANGELOG.md
git commit -m "docs: update roadmap, backlog, changelog for v0.6.0"
```
