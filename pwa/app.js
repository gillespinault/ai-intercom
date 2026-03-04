/* ================================================================
   Attention Hub — Tile Grid Control Room v3
   Stream Deck-style tile layout with stable positions.
   Each session = one tile. Waiting tiles highlighted.
   Positions stay consistent for visual/muscle memory.
   ================================================================ */

(function () {
  'use strict';

  // ---- State ----

  /** @type {WebSocket|null} */
  var ws = null;

  /** @type {'connected'|'reconnecting'|'disconnected'} */
  var connState = 'disconnected';

  var reconnectTimer = null;
  var reconnectDelay = 1000;
  var RECONNECT_MAX = 30000;

  /** @type {Array<Object>} */
  var sessions = [];

  var prefsOpen = false;

  /** Per-session event timeline: { sessionId: [{state, timestamp}] } */
  var sessionTimelines = {};

  /** Track previously known waiting sessions (for sound/vibration alerts) */
  var previousWaitingIds = {};

  /** Sent response tracking: { sessionId: timestamp } */
  var sentResponses = {};

  /** Currently open terminal slide session */
  var terminalSlideSessionId = null;

  /** Stable tile ordering: session IDs in insertion order */
  var sessionOrder = [];

  /** Dismissed tiles: hidden for this browser session only */
  var dismissedSessions = {};

  // ---- Audio Context (lazy init) ----

  var audioCtx = null;

  function getAudioCtx() {
    if (!audioCtx) {
      try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { /* no audio */ }
    }
    return audioCtx;
  }

  function playAlertSound() {
    var ctx = getAudioCtx();
    if (!ctx) return;
    if (ctx.state === 'suspended') ctx.resume();
    var now = ctx.currentTime;

    var osc1 = ctx.createOscillator();
    var gain1 = ctx.createGain();
    osc1.type = 'sine';
    osc1.frequency.value = 880;
    gain1.gain.setValueAtTime(0.15, now);
    gain1.gain.exponentialRampToValueAtTime(0.001, now + 0.1);
    osc1.connect(gain1);
    gain1.connect(ctx.destination);
    osc1.start(now);
    osc1.stop(now + 0.12);

    var osc2 = ctx.createOscillator();
    var gain2 = ctx.createGain();
    osc2.type = 'sine';
    osc2.frequency.value = 660;
    gain2.gain.setValueAtTime(0.12, now + 0.12);
    gain2.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
    osc2.connect(gain2);
    gain2.connect(ctx.destination);
    osc2.start(now + 0.12);
    osc2.stop(now + 0.38);
  }

  function triggerVibration() {
    if (navigator.vibrate) {
      navigator.vibrate([200, 100, 200]);
    }
  }

  // ---- Toast Notifications ----

  var TOAST_DURATION = 4000;

  function showToast(message, variant) {
    var container = document.getElementById('toast-container');
    if (!container) return;

    var icons = { success: '\u2713', warning: '\u26a0', error: '\u2717', info: '\u2139' };

    var toast = document.createElement('div');
    toast.className = 'toast toast-' + (variant || 'info');
    toast.innerHTML =
      '<span class="toast-icon">' + (icons[variant] || icons.info) + '</span>' +
      '<span class="toast-message">' + esc(message) + '</span>' +
      '<button class="toast-close" aria-label="Dismiss">\u00d7</button>';

    toast.querySelector('.toast-close').addEventListener('click', function () {
      dismissToast(toast);
    });

    container.appendChild(toast);
    setTimeout(function () { dismissToast(toast); }, TOAST_DURATION);
  }

  function dismissToast(toast) {
    if (toast.classList.contains('toast-exit')) return;
    toast.classList.add('toast-exit');
    setTimeout(function () {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 250);
  }

  // ---- Preferences ----

  var PREF_DEFAULTS = {
    'pref-permission': true,
    'pref-question': true,
    'pref-text-input': true,
    'pref-working': false,
    'pref-thinking': true,
    'pref-sound': false,
    'pref-vibrate': true,
    'pref-push': false,
    'pref-autoscroll': true,
    'pref-idle': true,
    'pref-eink': false
  };

  var prefs = {};

  function loadPrefs() {
    var keys = Object.keys(PREF_DEFAULTS);
    for (var i = 0; i < keys.length; i++) {
      var key = keys[i];
      var stored = localStorage.getItem('ah-' + key);
      prefs[key] = stored !== null ? stored === 'true' : PREF_DEFAULTS[key];
    }
  }

  function savePref(key, value) {
    prefs[key] = value;
    localStorage.setItem('ah-' + key, value ? 'true' : 'false');
  }

  function syncPrefsToDOM() {
    var keys = Object.keys(prefs);
    for (var i = 0; i < keys.length; i++) {
      var el = document.getElementById(keys[i]);
      if (el && el.type === 'checkbox') el.checked = prefs[keys[i]];
    }
    applyTheme();
  }

  function applyTheme() {
    if (prefs['pref-eink']) {
      document.documentElement.setAttribute('data-theme', 'eink');
      var meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.content = '#ffffff';
    } else {
      document.documentElement.removeAttribute('data-theme');
      var meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.content = '#0a0e1a';
    }
  }

  var PROMPT_TYPE_MAP = {
    'pref-permission': 'permission',
    'pref-question': 'question',
    'pref-text-input': 'text_input'
  };

  function initPrefsListeners() {
    var keys = Object.keys(PREF_DEFAULTS);
    for (var i = 0; i < keys.length; i++) {
      (function (key) {
        var el = document.getElementById(key);
        if (el) {
          el.addEventListener('change', function () {
            savePref(key, el.checked);
            if (key === 'pref-eink') applyTheme();
            // Sync prompt-type toggles to hub for Telegram filtering
            if (PROMPT_TYPE_MAP[key]) {
              var body = {};
              body[PROMPT_TYPE_MAP[key]] = el.checked;
              fetch('/api/attention/prefs', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
              }).catch(function() {});
            }
            renderDashboard();
          });
        }
      })(keys[i]);
    }
  }

  // ---- DOM refs ----

  var elContent = null;
  var elConnDot = null;
  var elPrefsBackdrop = null;
  var elPrefsPanel = null;
  var elHeaderStats = null;

  // ---- Helpers ----

  // Sentinel: idle_seconds above this threshold means the timestamp is bogus
  // (e.g. last_tool_time = 2000-01-01).
  var MAX_REASONABLE_IDLE = 86400; // 24 hours

  function formatAge(seconds) {
    if (seconds == null || seconds < 0) return '';
    if (seconds > MAX_REASONABLE_IDLE) return '24h+';
    if (seconds < 60) return seconds + 's';
    var m = Math.floor(seconds / 60);
    var s = seconds % 60;
    if (m < 60) return m + 'm' + (s ? String(s).padStart(2, '0') + 's' : '');
    var h = Math.floor(m / 60);
    var rm = m % 60;
    return h + 'h' + String(rm).padStart(2, '0') + 'm';
  }

  function esc(str) {
    if (!str) return '';
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function shortProject(path) {
    if (!path) return '';
    var parts = path.replace(/\/+$/, '').split('/');
    return parts[parts.length - 1] || path;
  }

  /**
   * Build a display name for a session.
   * Priority: session_name > project + tmux suffix for disambiguation.
   * e.g. "AI-intercom #2" when tmux is "cc-AI-intercom-2".
   */
  function sessionDisplayName(s) {
    if (s.session_name) return s.session_name;
    var proj = shortProject(s.project) || 'Session';
    // Clean skill: prefix for display (badge handles the label)
    if (proj.indexOf('skill:') === 0) proj = proj.substring(6);
    // Extract numeric suffix from tmux session name for disambiguation.
    // Only when tmux name matches "cc-<project>-<N>" pattern.
    if (s.tmux_session) {
      var pattern = new RegExp('^cc-' + proj.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '-(\\d+)$', 'i');
      var m = s.tmux_session.match(pattern);
      if (m) return proj + ' #' + m[1];
    }
    return proj;
  }

  function promptTypeIcon(type) {
    switch (type) {
      case 'permission': return '\ud83d\udd27';
      case 'question':   return '\u2753';
      case 'text_input': return '\u270d\ufe0f';
      default:           return '\u25cf';
    }
  }

  // ---- Session Filtering (stable order) ----

  function filterSessions(list) {
    return list.filter(function (s) {
      if (dismissedSessions[s.session_id]) return false;
      var state = (s.state || '').toLowerCase();
      var prompt = s.prompt;
      if (state === 'working' && !prefs['pref-working']) return false;
      if (state === 'thinking' && !prefs['pref-thinking']) return false;
      // Prompt type toggles only filter Telegram notifications (hub-side),
      // the dashboard always shows all waiting sessions regardless.
      return true;
    });
  }

  function getVisibleSessions() {
    // Maintain stable ordering: add new sessions, prune ended ones
    for (var i = 0; i < sessions.length; i++) {
      if (sessionOrder.indexOf(sessions[i].session_id) === -1) {
        sessionOrder.push(sessions[i].session_id);
      }
    }
    var activeIds = {};
    for (var j = 0; j < sessions.length; j++) {
      activeIds[sessions[j].session_id] = true;
    }
    sessionOrder = sessionOrder.filter(function (sid) { return activeIds[sid]; });

    // Filter by preferences, sort by stable insertion order
    var filtered = filterSessions(sessions);
    return filtered.slice().sort(function (a, b) {
      return sessionOrder.indexOf(a.session_id) - sessionOrder.indexOf(b.session_id);
    });
  }

  // ---- Header Stats ----

  function updateHeaderStats() {
    if (!elHeaderStats) return;
    var counts = { working: 0, thinking: 0, waiting: 0 };
    for (var i = 0; i < sessions.length; i++) {
      var st = (sessions[i].state || '').toLowerCase();
      if (counts[st] !== undefined) counts[st]++;
    }

    var html = '';
    if (counts.waiting > 0) {
      html += '<span class="stat-badge"><span class="stat-dot waiting"></span>' + counts.waiting + '</span>';
    }
    if (counts.thinking > 0) {
      html += '<span class="stat-badge"><span class="stat-dot thinking"></span>' + counts.thinking + '</span>';
    }
    if (counts.working > 0) {
      html += '<span class="stat-badge"><span class="stat-dot working"></span>' + counts.working + '</span>';
    }
    elHeaderStats.innerHTML = html;
  }

  // ---- Timeline Tracking ----

  function recordTimelineEvent(sessionId, state) {
    if (!sessionTimelines[sessionId]) {
      sessionTimelines[sessionId] = [];
    }
    var timeline = sessionTimelines[sessionId];
    if (timeline.length > 0 && timeline[timeline.length - 1].state === state) return;
    timeline.push({ state: state, timestamp: Date.now() });
    if (timeline.length > 10) timeline.shift();
  }

  // ---- Alert Logic ----

  function checkAlerts() {
    var currentWaitingIds = {};
    for (var i = 0; i < sessions.length; i++) {
      if ((sessions[i].state || '').toLowerCase() === 'waiting') {
        currentWaitingIds[sessions[i].session_id] = true;
      }
    }

    var newWaiting = false;
    var ids = Object.keys(currentWaitingIds);
    for (var j = 0; j < ids.length; j++) {
      if (!previousWaitingIds[ids[j]]) {
        newWaiting = true;
        break;
      }
    }

    if (newWaiting) {
      if (prefs['pref-sound']) playAlertSound();
      if (prefs['pref-vibrate']) triggerVibration();
    }

    previousWaitingIds = currentWaitingIds;
  }

  // ---- WebSocket ----

  function wsUrl() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + location.host + '/api/attention/ws';
  }

  function setConnState(state) {
    var prev = connState;
    connState = state;
    if (elConnDot) elConnDot.className = 'conn-dot ' + state;

    if (prev !== state) {
      if (state === 'connected' && prev !== 'connected') {
        showToast('Connected to hub', 'success');
      } else if (state === 'disconnected' && prev === 'connected') {
        showToast('Connection lost \u2014 reconnecting...', 'warning');
      }
    }
  }

  function connectWS() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

    setConnState('reconnecting');

    try {
      ws = new WebSocket(wsUrl());
    } catch (e) {
      scheduleReconnect();
      return;
    }

    ws.onopen = function () {
      setConnState('connected');
      reconnectDelay = 1000;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    };

    ws.onclose = function () {
      ws = null;
      setConnState('disconnected');
      scheduleReconnect();
    };

    ws.onerror = function () { /* onclose fires after */ };

    ws.onmessage = function (event) {
      try { var msg = JSON.parse(event.data); } catch (e) { return; }
      handleWSMessage(msg);
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    setConnState('reconnecting');
    reconnectTimer = setTimeout(function () {
      reconnectTimer = null;
      connectWS();
    }, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.5 + Math.random() * 500, RECONNECT_MAX);
  }

  function wsSend(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }

  function sendRespond(sessionId, keys) {
    wsSend({ action: 'respond', session_id: sessionId, keys: keys });
    sentResponses[sessionId] = Date.now();
    showToast('Response sent', 'success');
  }

  // ---- WS Message Handling ----

  function handleWSMessage(msg) {
    switch (msg.type) {
      case 'snapshot':
        sessions = msg.sessions || [];
        for (var i = 0; i < sessions.length; i++) {
          recordTimelineEvent(sessions[i].session_id, (sessions[i].state || '').toLowerCase());
        }
        checkAlerts();
        updateHeaderStats();
        renderDashboard();
        break;

      case 'session_update':
        upsertSession(msg.session);
        checkAlerts();
        updateHeaderStats();
        renderDashboard();
        break;

      case 'session_end':
        var endId = msg.session_id || (msg.session && msg.session.session_id);
        if (endId) {
          recordTimelineEvent(endId, 'ended');
          showToast('Session ended: ' + (getSessionName(endId) || endId), 'info');
        }
        removeSession(endId);
        checkAlerts();
        updateHeaderStats();
        renderDashboard();
        break;

      case 'prefs_updated':
        if (msg.prefs) {
          ['permission', 'question', 'text_input'].forEach(function(type) {
            var key = 'pref-' + type.replace('_', '-');
            var el = document.getElementById(key);
            if (el && msg.prefs[type] !== undefined) {
              el.checked = msg.prefs[type];
              savePref(key, msg.prefs[type]);
            }
          });
          renderDashboard();
        }
        break;

      default:
        if (msg.session) {
          upsertSession(msg.session);
          checkAlerts();
          updateHeaderStats();
          renderDashboard();
        }
        break;
    }
  }

  function getSessionState(sessionId) {
    for (var i = 0; i < sessions.length; i++) {
      if (sessions[i].session_id === sessionId) {
        return (sessions[i].state || '').toLowerCase();
      }
    }
    return null;
  }

  function getSessionName(sessionId) {
    for (var i = 0; i < sessions.length; i++) {
      if (sessions[i].session_id === sessionId) {
        return sessionDisplayName(sessions[i]);
      }
    }
    return null;
  }

  function upsertSession(sessionData) {
    if (!sessionData || !sessionData.session_id) return;
    var state = (sessionData.state || '').toLowerCase();
    recordTimelineEvent(sessionData.session_id, state);

    var idx = sessions.findIndex(function (s) { return s.session_id === sessionData.session_id; });
    if (idx >= 0) {
      sessions[idx] = sessionData;
    } else {
      sessions.push(sessionData);
      showToast('New session: ' + sessionDisplayName(sessionData), 'info');
    }
  }

  function removeSession(sessionId) {
    if (!sessionId) return;
    sessions = sessions.filter(function (s) { return s.session_id !== sessionId; });
    // Clean from stable order
    var idx = sessionOrder.indexOf(sessionId);
    if (idx >= 0) sessionOrder.splice(idx, 1);
    delete sentResponses[sessionId];
    if (terminalSlideSessionId === sessionId) closeTerminalSlide();
  }

  // ================================================================
  // Render Dashboard
  // ================================================================

  function renderDashboard() {
    updateHeaderStats();
    renderTileGrid();
  }

  // ================================================================
  // Tile Grid
  // ================================================================

  function renderTileGrid() {
    if (!elContent) return;
    if (window.TerminalManager) window.TerminalManager.beforeRender();

    var visible = getVisibleSessions();

    // Preserve input values before clearing
    var inputValues = {};
    var inputs = elContent.querySelectorAll('input.tile-text-input');
    for (var iv = 0; iv < inputs.length; iv++) {
      var parent = inputs[iv].closest('[data-session-id]');
      if (parent && inputs[iv].value) {
        inputValues[parent.dataset.sessionId] = inputs[iv].value;
      }
    }

    elContent.innerHTML = '';

    if (visible.length === 0 && sessions.length === 0) {
      elContent.className = 'main-content';
      renderEmpty();
      return;
    }

    if (visible.length === 0) {
      elContent.className = 'main-content';
      renderFilteredEmpty();
      return;
    }

    elContent.className = 'main-content tile-grid';

    for (var i = 0; i < visible.length; i++) {
      var tileEl = createTile(visible[i]);
      // Restore input value
      if (inputValues[visible[i].session_id]) {
        var inp = tileEl.querySelector('.tile-text-input');
        if (inp) inp.value = inputValues[visible[i].session_id];
      }
      elContent.appendChild(tileEl);
    }
  }

  function renderEmpty() {
    var div = document.createElement('div');
    div.className = 'empty-state';
    div.innerHTML =
      '<div class="empty-state-icon" aria-hidden="true">\u25C9</div>' +
      '<div class="empty-state-text">No active sessions.<br>Waiting for agents to connect.</div>';
    elContent.appendChild(div);
  }

  function renderFilteredEmpty() {
    var div = document.createElement('div');
    div.className = 'empty-state';
    div.innerHTML =
      '<div class="empty-state-icon" aria-hidden="true">\u29D6</div>' +
      '<div class="empty-state-text">' + sessions.length + ' session(s) hidden by filters or dismissed.<br>Adjust preferences or reload to see them.</div>';
    elContent.appendChild(div);
  }

  // ================================================================
  // Tile Creation
  // ================================================================

  function createTile(s) {
    var state = (s.state || 'idle').toLowerCase();
    var prompt = s.prompt;
    var hasTmux = !!s.tmux_session;
    var sessionName = sessionDisplayName(s);
    var age = (s.idle_seconds != null && prefs['pref-idle'] !== false) ? formatAge(s.idle_seconds) : '';

    var tile = document.createElement('div');
    var isStale = s.idle_seconds != null && s.idle_seconds > 1800; // >30 min
    var isSkill = (s.project || '').indexOf('skill:') === 0;
    var isSubagent = !s.tmux_session && !isSkill;
    var cls = 'tile state-' + state;
    if (isStale) cls += ' tile-stale';
    if (isSkill || isSubagent) cls += ' tile-background';
    tile.className = cls;
    tile.dataset.sessionId = s.session_id;

    var html = '';
    if (state === 'waiting' && prompt) {
      html = buildWaitingTile(s, prompt, sessionName, age, hasTmux);
    } else {
      html = buildIdleTile(s, sessionName, age, state);
    }

    tile.innerHTML = html;
    wireTileActions(tile, s);

    return tile;
  }

  function buildWaitingTile(s, prompt, sessionName, age, hasTmux) {
    var ptype = prompt.type || 'unknown';
    var html = '';

    // Header: project + tool badge + dismiss
    html += '<div class="tile-header">';
    html += '<span class="tile-name">' + esc(sessionName) + '</span>';
    if (prompt.tool) {
      html += '<span class="badge-tool">' + esc(prompt.tool) + '</span>';
    }
    if ((s.project || '').indexOf('skill:') === 0) {
      html += '<span class="badge-skill">skill</span>';
    } else if (!hasTmux) {
      html += '<span class="badge-monitoring">sub</span>';
    }
    html += '<button class="tile-dismiss" data-action="dismiss" title="Hide tile">\u00d7</button>';
    html += '</div>';

    // Meta: machine + age
    html += '<div class="tile-meta">';
    html += '<span>@' + esc(s.machine || '?') + '</span>';
    if (age) html += '<span class="tile-age">' + esc(age) + '</span>';
    html += '</div>';

    // Command/context preview (monospace code block)
    if (prompt.command_preview) {
      var preview = prompt.command_preview;
      var truncated = preview.length > 100 ? preview.substring(0, 97) + '\u2026' : preview;
      html += '<div class="tile-code">' + esc(truncated) + '</div>';
    }

    // Prompt question/description
    if (ptype === 'question' && prompt.question) {
      html += '<div class="tile-question">' + promptTypeIcon(ptype) + ' ' + esc(prompt.question) + '</div>';
    } else if (ptype === 'permission') {
      html += '<div class="tile-question">' + promptTypeIcon(ptype) + ' Allow?</div>';
    } else if (ptype === 'text_input') {
      html += '<div class="tile-question">\u276f Awaiting input</div>';
    }

    // Action buttons (only for tmux sessions)
    if (hasTmux) {
      html += buildTileActions(s, prompt);
    }

    return html;
  }

  function buildTileActions(s, prompt) {
    var ptype = prompt.type || '';
    var html = '';

    if (ptype === 'permission') {
      var choices = prompt.choices || [
        { key: 'y', label: 'Yes' },
        { key: 'n', label: 'No' }
      ];
      html += '<div class="tile-actions">';
      for (var i = 0; i < choices.length; i++) {
        var c = choices[i];
        var cls;
        if (c.key === 'y') cls = 'btn-allow';
        else if (c.key === 'n') cls = 'btn-deny';
        else if (c.key === 'a') cls = 'btn-always';
        else cls = 'btn-secondary';
        // Shorten long labels for tiles
        var shortLabel = c.label;
        if (shortLabel.length > 18) shortLabel = shortLabel.substring(0, 15) + '\u2026';
        html += '<button class="btn btn-sm ' + cls + '" data-action="respond" data-key="' + esc(c.key) + '" title="' + esc(c.label) + '">' + esc(shortLabel) + '</button>';
      }
      html += '</div>';

    } else if (ptype === 'question') {
      var qchoices = prompt.choices || [];
      var isSelectInput = qchoices.length > 0 && qchoices[0].key && qchoices[0].key.indexOf('select:') === 0;
      if (qchoices.length > 0) {
        html += '<div class="tile-actions">';
        for (var j = 0; j < qchoices.length; j++) {
          var qlabel = qchoices[j].label;
          if (qlabel.length > 18) qlabel = qlabel.substring(0, 15) + '\u2026';
          // Style SelectInput choices: first=allow, last with "no"=deny
          var qcls = 'btn-secondary';
          if (isSelectInput) {
            var lbl = qchoices[j].label.toLowerCase();
            if (lbl === 'yes' || lbl === 'ok' || lbl === 'confirm') qcls = 'btn-allow';
            else if (lbl.indexOf('no') === 0 || lbl === 'cancel' || lbl === 'deny') qcls = 'btn-deny';
          }
          html += '<button class="btn btn-sm ' + qcls + '" data-action="respond" data-key="' + esc(qchoices[j].key) + '" title="' + esc(qchoices[j].label) + '">' + esc(qlabel) + '</button>';
        }
        html += '</div>';
      }
      // Only show free text input for non-SelectInput questions
      if (!isSelectInput) {
        html += '<div class="tile-input-row">' +
          '<input type="text" class="input-text tile-text-input" placeholder="Type answer\u2026">' +
          '<button class="btn btn-sm btn-primary" data-action="send-text">\u23ce</button>' +
          '</div>';
      }

    } else if (ptype === 'text_input') {
      html += '<div class="tile-input-row">' +
        '<input type="text" class="input-text tile-text-input" placeholder="Type response\u2026">' +
        '<button class="btn btn-sm btn-primary" data-action="send-text">\u23ce</button>' +
        '</div>';
    }

    return html;
  }

  function buildIdleTile(s, sessionName, age, state) {
    var html = '';

    // Header: project + state dot + badges + dismiss
    html += '<div class="tile-header">';
    html += '<span class="tile-name">' + esc(sessionName) + '</span>';
    html += '<span class="tile-state-dot ' + esc(state) + '"></span>';
    if ((s.project || '').indexOf('skill:') === 0) {
      html += '<span class="badge-skill">skill</span>';
    } else if (!s.tmux_session) {
      html += '<span class="badge-monitoring">sub</span>';
    }
    html += '<button class="tile-dismiss" data-action="dismiss" title="Hide tile">\u00d7</button>';
    html += '</div>';

    // Meta
    html += '<div class="tile-meta">';
    html += '<span>@' + esc(s.machine || '?') + '</span>';
    if (age) html += '<span class="tile-age">' + esc(age) + '</span>';
    html += '</div>';

    // Last tool (clean up hook- prefix)
    if (s.last_tool) {
      var toolLabel = s.last_tool.replace(/^hook-/, '');
      html += '<div class="tile-last-tool">' + esc(toolLabel) + '</div>';
    }

    // State label
    html += '<div class="tile-state-label ' + esc(state) + '">' + esc(state.toUpperCase()) + '</div>';

    return html;
  }

  // ---- Tile Event Wiring ----

  function wireTileActions(tile, session) {
    tile.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-action]');
      if (!btn) {
        // Click on tile body: open terminal slide if tmux available
        if (!e.target.closest('input') && session.tmux_session) {
          openTerminalSlide(session.session_id);
        }
        return;
      }

      var action = btn.dataset.action;

      if (action === 'dismiss') {
        dismissedSessions[session.session_id] = true;
        renderDashboard();
        showToast('Tile hidden', 'info');
        return;
      }

      if (action === 'respond') {
        var key = btn.dataset.key;
        if (key) {
          sendRespond(session.session_id, key);
          tile.querySelectorAll('[data-action="respond"]').forEach(function (b) {
            b.disabled = true;
            b.classList.add('btn-sent');
          });
          btn.textContent = '\u2713 Sent';
          tile.classList.add('tile-sent');
        }
      }

      if (action === 'send-text') {
        var input = tile.querySelector('.tile-text-input');
        if (input && input.value.trim()) {
          sendRespond(session.session_id, input.value.trim() + '\n');
          input.disabled = true;
          btn.disabled = true;
          btn.textContent = '\u2713';
          tile.classList.add('tile-sent');
        }
      }
    });

    // Enter key in text input
    tile.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      var input = e.target;
      if (!input.classList.contains('tile-text-input')) return;
      e.preventDefault();
      var value = input.value.trim();
      if (!value) return;
      sendRespond(session.session_id, value + '\n');
      input.disabled = true;
      var sendBtn = input.parentElement.querySelector('[data-action]');
      if (sendBtn) { sendBtn.disabled = true; sendBtn.textContent = '\u2713'; }
      tile.classList.add('tile-sent');
    });
  }

  // ================================================================
  // Terminal Slide-Up Panel
  // ================================================================

  function openTerminalSlide(sessionId) {
    var slideEl = document.getElementById('terminal-slide');
    var headerEl = document.getElementById('terminal-slide-header');
    var bodyEl = document.getElementById('terminal-slide-body');
    if (!slideEl || !headerEl || !bodyEl) return;

    // Find session
    var session = null;
    for (var i = 0; i < sessions.length; i++) {
      if (sessions[i].session_id === sessionId) { session = sessions[i]; break; }
    }
    if (!session || !session.tmux_session) return;

    // Close existing if different
    if (terminalSlideSessionId && terminalSlideSessionId !== sessionId) {
      if (window.TerminalManager) window.TerminalManager.destroyTerminal(terminalSlideSessionId);
    }

    terminalSlideSessionId = sessionId;

    var sessionName = sessionDisplayName(session);
    headerEl.innerHTML =
      '<span>' + esc(session.machine || '') + ' / ' + esc(sessionName) + '</span>' +
      '<button class="terminal-slide-close" data-action="close-terminal">\u2715</button>';

    headerEl.querySelector('[data-action="close-terminal"]').addEventListener('click', closeTerminalSlide);

    bodyEl.innerHTML = '';

    if (window.TerminalManager) {
      window.TerminalManager.renderSingleTerminal(bodyEl, session);
    }

    slideEl.classList.add('open');
    slideEl.setAttribute('aria-hidden', 'false');

    // Wire swipe-down to close
    wireSlideSwipe(slideEl);
  }

  function closeTerminalSlide() {
    var slideEl = document.getElementById('terminal-slide');
    if (!slideEl) return;

    slideEl.classList.remove('open');
    slideEl.setAttribute('aria-hidden', 'true');

    if (terminalSlideSessionId && window.TerminalManager) {
      window.TerminalManager.destroyTerminal(terminalSlideSessionId);
    }
    terminalSlideSessionId = null;

    var bodyEl = document.getElementById('terminal-slide-body');
    if (bodyEl) bodyEl.innerHTML = '';
  }

  function wireSlideSwipe(slideEl) {
    var startY = 0;
    var handle = slideEl.querySelector('.terminal-slide-handle');
    if (!handle) return;

    handle.addEventListener('touchstart', function (e) {
      startY = e.touches[0].clientY;
    }, { passive: true });

    handle.addEventListener('touchend', function (e) {
      var dy = e.changedTouches[0].clientY - startY;
      if (dy > 50) closeTerminalSlide();
    }, { passive: true });
  }

  // ---- Preferences Panel ----

  function togglePrefs() {
    prefsOpen = !prefsOpen;
    if (elPrefsBackdrop) elPrefsBackdrop.classList.toggle('open', prefsOpen);
    if (elPrefsPanel) elPrefsPanel.classList.toggle('open', prefsOpen);
  }

  function closePrefs() {
    if (!prefsOpen) return;
    prefsOpen = false;
    if (elPrefsBackdrop) elPrefsBackdrop.classList.remove('open');
    if (elPrefsPanel) elPrefsPanel.classList.remove('open');
  }

  // ---- Service Worker ----

  function registerSW() {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/attention/sw.js', { scope: '/attention' }).catch(function () {});
    }
  }

  // ---- Init ----

  function init() {
    loadPrefs();

    elContent = document.getElementById('content');
    elConnDot = document.getElementById('conn-dot');
    elPrefsBackdrop = document.getElementById('prefs-backdrop');
    elPrefsPanel = document.getElementById('prefs-panel');
    elHeaderStats = document.getElementById('header-stats');

    syncPrefsToDOM();
    initPrefsListeners();

    var btnSettings = document.getElementById('btn-settings');
    if (btnSettings) btnSettings.addEventListener('click', togglePrefs);
    if (elPrefsBackdrop) elPrefsBackdrop.addEventListener('click', closePrefs);

    renderDashboard();
    connectWS();
    registerSW();

    // Fetch hub notification prefs and sync toggles
    fetch('/api/attention/prefs')
      .then(function(r) { return r.json(); })
      .then(function(hubPrefs) {
        ['permission', 'question', 'text_input'].forEach(function(type) {
          var key = 'pref-' + type.replace('_', '-');
          var el = document.getElementById(key);
          if (el && hubPrefs[type] !== undefined) {
            el.checked = hubPrefs[type];
            savePref(key, hubPrefs[type]);
          }
        });
        renderDashboard();
      })
      .catch(function() {}); // offline = use localStorage
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // ---- Public API ----

  window.AttentionHub = {
    sessions: function () { return sessions; },
    wsSend: wsSend,
    sendRespond: sendRespond,
    formatIdle: formatAge,
    connState: function () { return connState; },
    esc: esc,
    shortProject: shortProject,
    prefs: function () { return prefs; },
    getVisibleSessions: getVisibleSessions,
    renderDashboard: renderDashboard,
    createTile: createTile,
    showToast: showToast,
    openTerminalSlide: openTerminalSlide,
    closeTerminalSlide: closeTerminalSlide
  };

})();
