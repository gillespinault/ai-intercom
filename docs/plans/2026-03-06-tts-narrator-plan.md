# TTS Narrator Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add TTS voice announcements to the Attention Hub PWA, with both automatic event narration and Claude-pushed narrative messages via a new `intercom_announce` MCP tool.

**Architecture:** Hub proxies XTTS on Jetson Thor via `/api/tts`. PWA generates announcement text from WebSocket events + `tts_announce` messages, fetches audio from hub, plays via AudioContext. Claude Code sessions push rich narrative messages via new MCP tool.

**Tech Stack:** Python/FastAPI (hub), httpx (TTS proxy), vanilla JS (PWA AudioContext + fetch), existing XTTS HTTP API on Jetson Thor.

---

### Task 1: TTSAnnounce model

**Files:**
- Modify: `src/shared/models.py:185` (after AttentionEvent)
- Test: `tests/test_shared/test_attention_models.py`

**Step 1: Write the failing test**

```python
# tests/test_shared/test_attention_models.py — append at end

def test_tts_announce_model():
    from src.shared.models import TTSAnnounce
    a = TTSAnnounce(
        session_id="abc-123",
        project="coach-me",
        message="Phase 2 terminee",
        category="milestone",
    )
    assert a.session_id == "abc-123"
    assert a.priority == "normal"
    d = a.model_dump()
    assert d["category"] == "milestone"


def test_tts_announce_validation():
    from src.shared.models import TTSAnnounce
    import pytest
    with pytest.raises(Exception):
        TTSAnnounce(session_id="x", project="p", message="m", category="invalid")
```

**Step 2: Run test to verify it fails**

Run: `cd /home/gilles/serverlab/projects/AI-intercom && python -m pytest tests/test_shared/test_attention_models.py::test_tts_announce_model -v`
Expected: FAIL with ImportError (TTSAnnounce not defined)

**Step 3: Write minimal implementation**

Add to `src/shared/models.py` after the `AttentionEvent` class (around line 185):

```python
class TTSCategory(StrEnum):
    ATTENTION = "attention"
    PERMISSION = "permission"
    MILESTONE = "milestone"
    DIFFICULTY = "difficulty"
    LIFECYCLE = "lifecycle"
    DIDACTIC = "didactic"
    SUMMARY = "summary"


class TTSAnnounce(BaseModel):
    """A TTS announcement pushed by an agent via intercom_announce."""
    session_id: str
    project: str
    message: str
    category: TTSCategory = TTSCategory.MILESTONE
    priority: str = "normal"  # low | normal | high
```

**Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_shared/test_attention_models.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/shared/models.py tests/test_shared/test_attention_models.py
git commit -m "feat(tts): add TTSAnnounce and TTSCategory models"
```

---

### Task 2: Hub TTS proxy endpoint

**Files:**
- Modify: `src/hub/attention_api.py:410` (before `return router`)
- Modify: `src/shared/config.py` (add tts_url field if not present)
- Test: `tests/test_hub/test_attention_api_tts.py` (create)

**Step 1: Write the failing test**

```python
# tests/test_hub/test_attention_api_tts.py
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from src.hub.attention_store import AttentionStore
from src.hub.attention_api import create_attention_router
from fastapi import FastAPI


@pytest.fixture
def app():
    store = AttentionStore()
    registry = AsyncMock()
    app = FastAPI()
    router = create_attention_router(store, registry)
    app.include_router(router)
    # Inject tts_url into app state
    app.state.tts_url = "http://jetson-thor:8431"
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_tts_proxy_returns_audio(client):
    """POST /api/attention/tts should proxy to XTTS and return PCM audio."""
    fake_pcm = b"\x00\x01" * 100
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = fake_pcm
    mock_response.headers = {"content-type": "audio/raw"}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.post.return_value = mock_response
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        resp = client.post(
            "/api/attention/tts",
            json={"text": "Coach-me attend", "language": "fr"},
        )
        assert resp.status_code == 200
        assert resp.content == fake_pcm


def test_tts_proxy_rejects_empty_text(client):
    resp = client.post("/api/attention/tts", json={"text": "", "language": "fr"})
    assert resp.status_code == 400


def test_tts_proxy_rate_limited(client):
    """Second request within 2s should get 429."""
    fake_pcm = b"\x00\x01" * 100
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = fake_pcm
    mock_response.headers = {"content-type": "audio/raw"}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.post.return_value = mock_response
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        MockClient.return_value = instance

        resp1 = client.post("/api/attention/tts", json={"text": "test1", "language": "fr"})
        assert resp1.status_code == 200

        resp2 = client.post("/api/attention/tts", json={"text": "test2", "language": "fr"})
        assert resp2.status_code == 429
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hub/test_attention_api_tts.py -v`
Expected: FAIL (no /api/attention/tts endpoint)

**Step 3: Write minimal implementation**

Add to `src/hub/attention_api.py`, inside `create_attention_router()` before `return router` (around line 409):

```python
    # ------------------------------------------------------------------
    # TTS proxy endpoint
    # ------------------------------------------------------------------

    _tts_last_request_time: dict[str, float] = {}
    _TTS_RATE_LIMIT_SECONDS = 2.0

    @router.post("/tts")
    async def tts_proxy(request: Request):
        """Proxy TTS synthesis to the XTTS service on Jetson Thor."""
        import time

        data = await request.json()
        text = data.get("text", "").strip()
        language = data.get("language", "fr")

        if not text:
            return Response(status_code=400, content="text is required")

        # Rate limiting
        now = time.monotonic()
        last = _tts_last_request_time.get("last", 0)
        if now - last < _TTS_RATE_LIMIT_SECONDS:
            return Response(status_code=429, content="Rate limited")
        _tts_last_request_time["last"] = now

        # Get TTS URL from app state (set in hub_api.py)
        tts_url = request.app.state.tts_url if hasattr(request.app.state, "tts_url") else ""
        if not tts_url:
            return Response(status_code=503, content="TTS service not configured")

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{tts_url}/v1/tts",
                    json={"text": text, "language": language, "sample_rate": 24000},
                )
                resp.raise_for_status()
                return Response(
                    content=resp.content,
                    media_type="audio/raw",
                    headers={"Content-Type": "audio/raw"},
                )
        except httpx.HTTPError as e:
            logger.warning("TTS proxy error: %s", e)
            return Response(status_code=502, content=f"TTS error: {e}")
```

Then in `src/hub/hub_api.py`, after `attention_store = AttentionStore()` (around line 48), add:

```python
    # Wire TTS URL from config
    tts_url = getattr(config, 'voice', None)
    if tts_url and hasattr(tts_url, 'tts_url'):
        app.state.tts_url = tts_url.tts_url
    elif hasattr(config, 'voice') and isinstance(config.voice, dict):
        app.state.tts_url = config.voice.get("tts_url", "")
    else:
        app.state.tts_url = ""
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_hub/test_attention_api_tts.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/hub/attention_api.py src/hub/hub_api.py tests/test_hub/test_attention_api_tts.py
git commit -m "feat(tts): add /api/attention/tts proxy endpoint with rate limiting"
```

---

### Task 3: Hub announce endpoint + broadcast

**Files:**
- Modify: `src/hub/attention_api.py` (add POST /announce inside router)
- Test: `tests/test_hub/test_attention_api_tts.py` (append)

**Step 1: Write the failing test**

Append to `tests/test_hub/test_attention_api_tts.py`:

```python
def test_announce_endpoint_broadcasts(client, app):
    """POST /api/attention/announce should broadcast tts_announce event."""
    import asyncio
    broadcast_calls = []
    store = app.state.attention_store if hasattr(app.state, "attention_store") else None

    # We just test the HTTP response; broadcast testing is via attention_store tests
    resp = client.post("/api/attention/announce", json={
        "machine_id": "serverlab",
        "session_id": "sess-1",
        "project": "coach-me",
        "message": "Phase 2 terminee",
        "category": "milestone",
        "priority": "normal",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_announce_rejects_empty_message(client):
    resp = client.post("/api/attention/announce", json={
        "machine_id": "serverlab",
        "session_id": "s1",
        "project": "p",
        "message": "",
        "category": "milestone",
    })
    assert resp.status_code == 400
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hub/test_attention_api_tts.py::test_announce_endpoint_broadcasts -v`
Expected: FAIL (404, endpoint doesn't exist)

**Step 3: Write minimal implementation**

Add to `src/hub/attention_api.py`, inside `create_attention_router()`, before the TTS proxy section:

```python
    # ------------------------------------------------------------------
    # TTS announce endpoint (from agents via daemon)
    # ------------------------------------------------------------------

    @router.post("/announce")
    async def receive_announce(request: Request):
        """Receive a TTS announcement from an agent and broadcast to PWA."""
        data = await request.json()
        message = data.get("message", "").strip()
        if not message:
            return Response(status_code=400, content="message is required")

        broadcast_payload = {
            "type": "tts_announce",
            "session_id": data.get("session_id", ""),
            "project": data.get("project", ""),
            "message": message,
            "category": data.get("category", "milestone"),
            "priority": data.get("priority", "normal"),
            "machine_id": data.get("machine_id", ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await store.broadcast(broadcast_payload)
        return {"status": "ok"}
```

Also update the fixture in the test to wire `attention_store` on app.state:

```python
@pytest.fixture
def app():
    store = AttentionStore()
    registry = AsyncMock()
    app = FastAPI()
    router = create_attention_router(store, registry)
    app.include_router(router)
    app.state.tts_url = "http://jetson-thor:8431"
    app.state.attention_store = store  # for announce endpoint
    return app
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_hub/test_attention_api_tts.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/hub/attention_api.py tests/test_hub/test_attention_api_tts.py
git commit -m "feat(tts): add /api/attention/announce endpoint with broadcast"
```

---

### Task 4: Daemon hub_client.push_announce()

**Files:**
- Modify: `src/daemon/hub_client.py:218` (after push_usage_stats)
- Test: `tests/test_daemon/test_hub_client.py` (append)

**Step 1: Write the failing test**

Append to `tests/test_daemon/test_hub_client.py`:

```python
async def test_push_announce(hub_client, mock_post):
    """push_announce() should POST to /api/attention/announce."""
    mock_post.return_value = httpx.Response(200, json={"status": "ok"})
    result = await hub_client.push_announce(
        session_id="sess-1",
        project="coach-me",
        message="Phase 2 done",
        category="milestone",
        priority="normal",
    )
    assert result["status"] == "ok"
    call_args = mock_post.call_args
    body = json.loads(call_args.kwargs.get("content", call_args.args[1] if len(call_args.args) > 1 else b"{}"))
    assert body["message"] == "Phase 2 done"
    assert body["category"] == "milestone"
    assert "/api/attention/announce" in str(call_args)
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_daemon/test_hub_client.py::test_push_announce -v`
Expected: FAIL (AttributeError: push_announce not found)

**Step 3: Write minimal implementation**

Add to `src/daemon/hub_client.py` after `push_usage_stats` (around line 218):

```python
    async def push_announce(
        self,
        session_id: str,
        project: str,
        message: str,
        category: str = "milestone",
        priority: str = "normal",
    ) -> dict:
        """Push a TTS announcement to the hub for PWA broadcast."""
        return await self._post("/api/attention/announce", {
            "machine_id": self.machine_id,
            "session_id": session_id,
            "project": project,
            "message": message,
            "category": category,
            "priority": priority,
        })
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_daemon/test_hub_client.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/daemon/hub_client.py tests/test_daemon/test_hub_client.py
git commit -m "feat(tts): add push_announce() to HubClient"
```

---

### Task 5: MCP tool intercom_announce

**Files:**
- Modify: `src/daemon/mcp_server.py` (add announce method to IntercomTools + MCP tool)
- Test: `tests/test_daemon/test_mcp_server.py` (append)

**Step 1: Write the failing test**

Append to `tests/test_daemon/test_mcp_server.py`:

```python
async def test_announce(tools):
    """intercom_announce should call push_announce on hub_client."""
    tools.hub_client.push_announce = AsyncMock(return_value={"status": "ok"})
    tools._session_id = "sess-123"
    result = await tools.announce(
        message="Phase 2 terminee",
        category="milestone",
        priority="normal",
    )
    assert result["status"] == "ok"
    tools.hub_client.push_announce.assert_called_once_with(
        session_id="sess-123",
        project="infra",
        message="Phase 2 terminee",
        category="milestone",
        priority="normal",
    )
```

**Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_daemon/test_mcp_server.py::test_announce -v`
Expected: FAIL (AttributeError: announce not found)

**Step 3: Write minimal implementation**

Add to `IntercomTools` class in `src/daemon/mcp_server.py` (after `check_inbox`):

```python
    async def announce(
        self,
        message: str,
        category: str = "milestone",
        priority: str = "normal",
    ) -> dict:
        """Push a TTS announcement to the hub for voice narration."""
        return await self.hub_client.push_announce(
            session_id=self._session_id or "",
            project=self.current_project,
            message=message,
            category=category,
            priority=priority,
        )
```

Add the MCP tool in `create_mcp_server()` (after `intercom_check_inbox`):

```python
    @mcp.tool()
    async def intercom_announce(
        message: str,
        category: str = "milestone",
        priority: str = "normal",
    ) -> dict:
        """Announce progress via TTS voice narration in the Attention Hub PWA.

        Use this to narrate major milestones, difficulties, or explain what
        you're working on. The message will be synthesized as speech and
        played in the user's browser.

        Args:
            message: The announcement text (French, max 200 chars, conversational).
            category: "milestone" (plan phase done), "difficulty" (blocked/retrying),
                      or "didactic" (explain current work).
            priority: "low", "normal", or "high".
        """
        return await tools.announce(
            message=message, category=category, priority=priority
        )
```

**Step 4: Run tests**

Run: `python -m pytest tests/test_daemon/test_mcp_server.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/daemon/mcp_server.py tests/test_daemon/test_mcp_server.py
git commit -m "feat(tts): add intercom_announce MCP tool"
```

---

### Task 6: PWA TTS module

**Files:**
- Create: `pwa/tts.js`
- Modify: `pwa/index.html` (add script tag + settings UI)
- Modify: `pwa/app.js` (wire TTS module into WebSocket handler)

**Step 1: Create the TTS module**

```javascript
// pwa/tts.js — TTS Narrator module for Attention Hub PWA
(function () {
  'use strict';

  // ---- Settings (localStorage) ----

  var DEFAULTS = {
    enabled: true,
    volume: 0.8,
    verbosity: 'informatif',  // 'minimal' | 'informatif'
    cooldown: 5,              // seconds between announcements
    categories: {
      attention: true,
      permission: true,
      milestone: true,
      difficulty: true,
      lifecycle: false,
      didactic: false,
      summary: false,
    },
  };

  function loadSettings() {
    try {
      var saved = JSON.parse(localStorage.getItem('tts_settings') || '{}');
      var s = Object.assign({}, DEFAULTS, saved);
      s.categories = Object.assign({}, DEFAULTS.categories, saved.categories || {});
      return s;
    } catch (e) {
      return Object.assign({}, DEFAULTS);
    }
  }

  function saveSettings(s) {
    localStorage.setItem('tts_settings', JSON.stringify(s));
  }

  var settings = loadSettings();

  // ---- Queue ----

  var queue = [];
  var playing = false;
  var lastPlayTime = 0;
  var recentMessages = {};  // message -> timestamp (dedup)

  function enqueue(text, category, priority) {
    if (!settings.enabled) return;
    if (!settings.categories[category]) return;

    // Dedup: skip if same message within 10s
    var now = Date.now();
    if (recentMessages[text] && now - recentMessages[text] < 10000) return;
    recentMessages[text] = now;

    // Clean old dedup entries
    Object.keys(recentMessages).forEach(function (k) {
      if (now - recentMessages[k] > 15000) delete recentMessages[k];
    });

    queue.push({ text: text, category: category, priority: priority, time: now });

    // Sort by priority (high first)
    var prio = { high: 0, normal: 1, low: 2 };
    queue.sort(function (a, b) { return (prio[a.priority] || 1) - (prio[b.priority] || 1); });

    processQueue();
  }

  function processQueue() {
    if (playing || queue.length === 0) return;

    var now = Date.now();
    var cooldownMs = settings.cooldown * 1000;
    if (now - lastPlayTime < cooldownMs) {
      setTimeout(processQueue, cooldownMs - (now - lastPlayTime) + 100);
      return;
    }

    playing = true;
    var item = queue.shift();
    playTTS(item.text, function () {
      lastPlayTime = Date.now();
      playing = false;
      processQueue();
    });
  }

  // ---- Audio playback ----

  var audioCtx = null;

  function getAudioCtx() {
    if (!audioCtx) {
      try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) {}
    }
    return audioCtx;
  }

  function playTTS(text, onDone) {
    fetch('/api/attention/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text, language: 'fr' }),
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error('TTS ' + resp.status);
        return resp.arrayBuffer();
      })
      .then(function (pcmBuffer) {
        var ctx = getAudioCtx();
        if (!ctx) { onDone(); return; }
        if (ctx.state === 'suspended') ctx.resume();

        // PCM 16-bit mono 24kHz -> Float32 AudioBuffer
        var int16 = new Int16Array(pcmBuffer);
        var float32 = new Float32Array(int16.length);
        for (var i = 0; i < int16.length; i++) {
          float32[i] = int16[i] / 32768.0;
        }

        var audioBuffer = ctx.createBuffer(1, float32.length, 24000);
        audioBuffer.getChannelData(0).set(float32);

        var source = ctx.createBufferSource();
        var gainNode = ctx.createGain();
        gainNode.gain.value = settings.volume;

        source.buffer = audioBuffer;
        source.connect(gainNode);
        gainNode.connect(ctx.destination);
        source.onended = onDone;
        source.start(0);
      })
      .catch(function (err) {
        console.warn('[TTS] playback error:', err);
        onDone();
      });
  }

  // ---- Template generation ----

  function templateMinimal(project, type, data) {
    switch (type) {
      case 'waiting_permission': return project + ' permission';
      case 'waiting_question': return project + ' question';
      case 'waiting_input': return project + ' attend';
      case 'new_session': return project + ' demarre';
      case 'session_ended': return project + ' termine';
      default: return project;
    }
  }

  function templateInformatif(project, type, data) {
    switch (type) {
      case 'waiting_permission':
        var tool = (data && data.tool) || '';
        return project + ' demande la permission' + (tool ? ' pour ' + tool : '');
      case 'waiting_question':
        return project + ' pose une question';
      case 'waiting_input':
        return project + ' attend ton input';
      case 'new_session':
        return project + ' demarre';
      case 'session_ended':
        return project + ' termine';
      default:
        return project;
    }
  }

  function generateText(project, eventType, data) {
    if (settings.verbosity === 'minimal') {
      return templateMinimal(project, eventType, data);
    }
    return templateInformatif(project, eventType, data);
  }

  // ---- Public API (called from app.js) ----

  window.AttentionTTS = {
    /** Process a WebSocket event and possibly enqueue a TTS announcement. */
    handleEvent: function (event) {
      if (!settings.enabled) return;

      var type = event.type;
      var session = event.session;

      if (type === 'tts_announce') {
        // Narrative announcement from Claude
        enqueue(event.message, event.category || 'milestone', event.priority || 'normal');
        return;
      }

      if (!session) return;
      var project = session.project || 'session';

      if (type === 'state_changed' && session.state === 'waiting') {
        var prompt = session.prompt || {};
        var ptype = prompt.type || 'text_input';
        if (ptype === 'permission') {
          enqueue(
            generateText(project, 'waiting_permission', { tool: prompt.tool }),
            'permission', 'normal'
          );
        } else if (ptype === 'question') {
          enqueue(generateText(project, 'waiting_question', {}), 'attention', 'normal');
        } else {
          enqueue(generateText(project, 'waiting_input', {}), 'attention', 'normal');
        }
      } else if (type === 'new_session') {
        enqueue(generateText(project, 'new_session', {}), 'lifecycle', 'low');
      } else if (type === 'session_ended') {
        enqueue(generateText(project, 'session_ended', {}), 'lifecycle', 'low');
      }
    },

    /** Return current settings (for UI). */
    getSettings: function () { return settings; },

    /** Update settings (partial merge). */
    updateSettings: function (updates) {
      if (updates.categories) {
        settings.categories = Object.assign(settings.categories, updates.categories);
        delete updates.categories;
      }
      Object.assign(settings, updates);
      saveSettings(settings);
    },

    /** Force resume AudioContext (call on user gesture). */
    unlockAudio: function () {
      var ctx = getAudioCtx();
      if (ctx && ctx.state === 'suspended') ctx.resume();
    },
  };
})();
```

**Step 2: Add script tag to index.html**

In `pwa/index.html`, before the `app.js` script tag (line 193), add:

```html
  <script defer src="/attention/tts.js?v=1"></script>
```

**Step 3: Add TTS settings section to index.html**

In `pwa/index.html`, inside the prefs-panel (after the "Dashboard" section, around line 146), add:

```html
    <div class="prefs-section">
      <div class="prefs-label">
        <svg class="prefs-label-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 5L6 9H2v6h4l5 4V5z"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>
        Voice (TTS)
      </div>
      <div class="prefs-hint">Annonces vocales via XTTS</div>
      <div class="prefs-row">
        <span class="prefs-row-text">Enabled</span>
        <label class="toggle">
          <input type="checkbox" id="pref-tts-enabled">
          <span class="toggle-track"></span>
        </label>
      </div>
      <div class="prefs-row">
        <span class="prefs-row-text">Verbosity</span>
        <select id="pref-tts-verbosity" class="prefs-select">
          <option value="minimal">Minimal</option>
          <option value="informatif">Informatif</option>
        </select>
      </div>
      <div class="prefs-row">
        <span class="prefs-row-text">Volume</span>
        <input type="range" id="pref-tts-volume" min="0" max="1" step="0.1" value="0.8" class="prefs-range">
      </div>
      <div class="prefs-row">
        <span class="prefs-row-text">Cooldown (s)</span>
        <input type="number" id="pref-tts-cooldown" min="1" max="30" value="5" class="prefs-number" style="width:60px">
      </div>
      <div class="prefs-row"><span class="prefs-row-text">Attention</span><label class="toggle"><input type="checkbox" id="pref-tts-attention"><span class="toggle-track"></span></label></div>
      <div class="prefs-row"><span class="prefs-row-text">Permission</span><label class="toggle"><input type="checkbox" id="pref-tts-permission"><span class="toggle-track"></span></label></div>
      <div class="prefs-row"><span class="prefs-row-text">Milestones</span><label class="toggle"><input type="checkbox" id="pref-tts-milestone"><span class="toggle-track"></span></label></div>
      <div class="prefs-row"><span class="prefs-row-text">Difficulties</span><label class="toggle"><input type="checkbox" id="pref-tts-difficulty"><span class="toggle-track"></span></label></div>
      <div class="prefs-row"><span class="prefs-row-text">Lifecycle</span><label class="toggle"><input type="checkbox" id="pref-tts-lifecycle"><span class="toggle-track"></span></label></div>
      <div class="prefs-row"><span class="prefs-row-text">Didactic</span><label class="toggle"><input type="checkbox" id="pref-tts-didactic"><span class="toggle-track"></span></label></div>
    </div>
```

**Step 4: Wire TTS into app.js WebSocket handler**

In `pwa/app.js`, find the WebSocket `onmessage` handler where events are dispatched. After `handleWsMessage(event)` processes the event, add:

```javascript
    // TTS narration
    if (window.AttentionTTS) {
      window.AttentionTTS.handleEvent(msg);
    }
```

Also add TTS settings wiring in the preferences init/save code:

```javascript
    // TTS prefs: load
    if (window.AttentionTTS) {
      var ttsS = window.AttentionTTS.getSettings();
      setChecked('pref-tts-enabled', ttsS.enabled);
      setValue('pref-tts-verbosity', ttsS.verbosity);
      setValue('pref-tts-volume', ttsS.volume);
      setValue('pref-tts-cooldown', ttsS.cooldown);
      setChecked('pref-tts-attention', ttsS.categories.attention);
      setChecked('pref-tts-permission', ttsS.categories.permission);
      setChecked('pref-tts-milestone', ttsS.categories.milestone);
      setChecked('pref-tts-difficulty', ttsS.categories.difficulty);
      setChecked('pref-tts-lifecycle', ttsS.categories.lifecycle);
      setChecked('pref-tts-didactic', ttsS.categories.didactic);
    }
```

And in the settings change handlers:

```javascript
    // TTS prefs: save
    bindChange('pref-tts-enabled', function (v) { window.AttentionTTS.updateSettings({ enabled: v }); });
    bindChange('pref-tts-verbosity', function (v) { window.AttentionTTS.updateSettings({ verbosity: v }); });
    bindChange('pref-tts-volume', function (v) { window.AttentionTTS.updateSettings({ volume: parseFloat(v) }); });
    bindChange('pref-tts-cooldown', function (v) { window.AttentionTTS.updateSettings({ cooldown: parseInt(v, 10) }); });
    bindChange('pref-tts-attention', function (v) { window.AttentionTTS.updateSettings({ categories: { attention: v } }); });
    bindChange('pref-tts-permission', function (v) { window.AttentionTTS.updateSettings({ categories: { permission: v } }); });
    bindChange('pref-tts-milestone', function (v) { window.AttentionTTS.updateSettings({ categories: { milestone: v } }); });
    bindChange('pref-tts-difficulty', function (v) { window.AttentionTTS.updateSettings({ categories: { difficulty: v } }); });
    bindChange('pref-tts-lifecycle', function (v) { window.AttentionTTS.updateSettings({ categories: { lifecycle: v } }); });
    bindChange('pref-tts-didactic', function (v) { window.AttentionTTS.updateSettings({ categories: { didactic: v } }); });
```

Also add `AttentionTTS.unlockAudio()` to the first user interaction handler (click/touch) to unlock AudioContext on mobile.

**Step 5: Commit**

```bash
git add pwa/tts.js pwa/index.html pwa/app.js
git commit -m "feat(tts): add PWA TTS narrator module with settings UI"
```

---

### Task 7: Wire TTS URL from config to hub

**Files:**
- Modify: `src/shared/config.py` (ensure voice.tts_url is parsed)
- Modify: `src/hub/hub_api.py` (pass tts_url to app state)

**Step 1: Check config parsing**

Read `src/shared/config.py` to see how `voice` config is currently parsed. The config already has:
```yaml
voice:
  tts_url: "http://jetson-thor:8431/v1/tts"
```

Ensure `IntercomConfig` has a `voice` dict or dataclass that includes `tts_url`. If it's already a dict pass-through, just wire it.

**Step 2: Wire in hub_api.py**

In `src/hub/hub_api.py`, after creating the attention store (line ~48), add:

```python
    # Wire TTS URL for the proxy endpoint
    voice_cfg = getattr(config, 'voice', None) or {}
    if isinstance(voice_cfg, dict):
        app.state.tts_url = voice_cfg.get("tts_url", "")
    elif hasattr(voice_cfg, 'tts_url'):
        app.state.tts_url = voice_cfg.tts_url
    else:
        app.state.tts_url = ""
```

**Step 3: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All existing + new tests PASS

**Step 4: Commit**

```bash
git add src/hub/hub_api.py src/shared/config.py
git commit -m "feat(tts): wire voice.tts_url from config to hub app state"
```

---

### Task 8: CLAUDE.md update + final integration test

**Files:**
- Modify: `CLAUDE.md` (add TTS Announcements section)
- Manual test: deploy and verify end-to-end

**Step 1: Update CLAUDE.md**

Add to `CLAUDE.md` after the "Attention Pipeline Details" section:

```markdown
## TTS Announcements

When working on multi-step plans, use `intercom_announce()` to narrate progress:
- **milestone**: completing a plan phase, tests passing, deployment done
- **difficulty**: 3rd retry on a failing test, API error, blocked on something
- **didactic**: brief explanation of current work (only if user enabled it)

Keep messages under 200 chars, in French, conversational tone.
Examples: "Phase 2 terminee, tous les tests passent", "Bloque sur un test flaky, je retente"
```

**Step 2: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

**Step 3: Deploy and manual test**

```bash
# Rebuild hub
docker compose -f docker-compose.hub.yml build --no-cache
docker compose -f docker-compose.hub.yml up -d

# Reinstall daemon
/home/gilles/.local/share/ai-intercom-daemon/venv/bin/pip install -e .
sudo systemctl restart ai-intercom-daemon

# Test TTS proxy manually
curl -X POST https://attention.robotsinlove.be/api/attention/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Test de synthese vocale", "language": "fr"}' \
  --output /tmp/test.pcm

# Test announce endpoint
curl -X POST https://attention.robotsinlove.be/api/attention/announce \
  -H "Content-Type: application/json" \
  -d '{"machine_id":"serverlab","session_id":"test","project":"test","message":"Ceci est un test","category":"milestone"}'
```

Open the PWA, enable TTS in settings, verify audio plays on announcements.

**Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add TTS announcement instructions to CLAUDE.md"
```
