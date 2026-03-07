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

  /** Per-session context stats from usage_stats messages */
  var sessionContextStats = {};

  /** Pending permission requests: { request_id: {...} } */
  var pendingPermissions = {};

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
    'pref-eink': false,
    'pref-group-by': 'none',  // 'none' | 'project' | 'machine'
    'pref-conversation-active': true,
    'pref-show-agent-exchanges': true,
    'pref-voice-response': true,
    'pref-auto-print-pos': false,
    'pref-hear-agents': false
  };

  var prefs = {};

  var STRING_PREFS = { 'pref-group-by': true };

  function loadPrefs() {
    var keys = Object.keys(PREF_DEFAULTS);
    for (var i = 0; i < keys.length; i++) {
      var key = keys[i];
      var stored = localStorage.getItem('ah-' + key);
      if (STRING_PREFS[key]) {
        prefs[key] = stored !== null ? stored : PREF_DEFAULTS[key];
      } else {
        prefs[key] = stored !== null ? stored === 'true' : PREF_DEFAULTS[key];
      }
    }
  }

  function savePref(key, value) {
    prefs[key] = value;
    if (STRING_PREFS[key]) {
      localStorage.setItem('ah-' + key, value);
    } else {
      localStorage.setItem('ah-' + key, value ? 'true' : 'false');
    }
  }

  function syncPrefsToDOM() {
    var keys = Object.keys(prefs);
    for (var i = 0; i < keys.length; i++) {
      var el = document.getElementById(keys[i]);
      if (!el) continue;
      if (el.type === 'checkbox') el.checked = prefs[keys[i]];
      else if (el.tagName === 'SELECT') el.value = prefs[keys[i]];
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
            if (STRING_PREFS[key]) {
              savePref(key, el.value);
            } else {
              savePref(key, el.checked);
            }
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

  function formatToolInput(toolName, toolInput) {
    if (!toolInput) return '';
    if (toolName === 'Bash' && toolInput.command) return toolInput.command;
    if (toolName === 'Edit' && toolInput.file_path) return toolInput.file_path;
    if (toolName === 'Write' && toolInput.file_path) return toolInput.file_path;
    if (toolName === 'Read' && toolInput.file_path) return toolInput.file_path;
    return JSON.stringify(toolInput).substring(0, 120);
  }

  function sendPermissionDecision(requestId, decision) {
    wsSend({ action: 'permission_decide', request_id: requestId, decision: decision });
    delete pendingPermissions[requestId];
    showToast(decision === 'allow' ? 'Allowed' : 'Denied', decision === 'allow' ? 'success' : 'warning');
    renderDashboard();
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
    var badgesEl = document.getElementById('stat-badges');
    if (!badgesEl) return;
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
    var permCount = Object.keys(pendingPermissions).length;
    if (permCount > 0) {
      html += '<span class="stat-badge"><span class="stat-dot permission"></span>' + permCount + '</span>';
    }
    badgesEl.innerHTML = html;
  }

  // ---- Usage Stats (block progress, reset countdown, weekly tokens) ----

  function updateUsageStats(stats) {
    if (!stats) return;

    // Block bar
    var block = stats.block || {};
    var blockFill = document.getElementById('usage-block-fill');
    var blockLabel = document.getElementById('usage-block-label');
    if (blockFill) {
      if (block.is_active) {
        var pct = Math.min(100, Math.max(0, block.elapsed_pct || 0));
        blockFill.style.width = pct + '%';
        blockFill.className = 'usage-bar-fill' + (pct > 80 ? ' crit' : pct > 50 ? ' warn' : '');
        if (blockLabel) blockLabel.textContent = Math.round(pct) + '%';
      } else {
        blockFill.style.width = '0%';
        blockFill.className = 'usage-bar-fill';
        if (blockLabel) blockLabel.textContent = '--';
      }
    }

    // Reset countdown
    var resetEl = document.getElementById('usage-reset');
    if (resetEl) {
      if (block.is_active && block.remaining_minutes != null) {
        var h = Math.floor(block.remaining_minutes / 60);
        var m = block.remaining_minutes % 60;
        var countdown = h > 0 ? h + 'h' + String(m).padStart(2, '0') : m + 'm';
        resetEl.textContent = countdown + ' \u2192 ' + (block.reset_time || '--:--');
      } else {
        resetEl.textContent = '--:--';
      }
    }

    // Weekly tokens
    var weeklyEl = document.getElementById('usage-weekly');
    if (weeklyEl && stats.weekly) {
      weeklyEl.textContent = 'W: ' + (stats.weekly.display || '--');
    }

    // Per-session context
    if (stats.sessions) {
      for (var sid in stats.sessions) {
        sessionContextStats[sid] = stats.sessions[sid];
      }
    }
    updateAllContextBars();
  }

  // ---- Per-Session Context Bar ----

  function renderContextBarHTML(sessionId) {
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
      var existing = tile.querySelector('.context-bar');
      if (!existing) {
        // Insert context bar at the end of tile content
        tile.insertAdjacentHTML('beforeend', renderContextBarHTML(sid));
      } else {
        // Update existing bar
        var ctx = sessionContextStats[sid];
        var pct = ctx ? Math.min(100, ctx.context_percent || 0) : 0;
        var fill = existing.querySelector('.context-bar-fill');
        var label = existing.querySelector('.context-bar-label');
        if (fill) {
          fill.style.width = pct + '%';
          fill.className = 'context-bar-fill' + (pct > 80 ? ' crit' : pct > 50 ? ' warn' : '');
        }
        if (label) label.textContent = Math.round(pct) + '%';
      }
    }
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
        // Load pending permissions from snapshot
        pendingPermissions = {};
        if (msg.pending_permissions) {
          for (var pp = 0; pp < msg.pending_permissions.length; pp++) {
            var perm = msg.pending_permissions[pp];
            pendingPermissions[perm.request_id] = perm;
          }
        }
        for (var i = 0; i < sessions.length; i++) {
          recordTimelineEvent(sessions[i].session_id, (sessions[i].state || '').toLowerCase());
        }
        if (msg.usage_stats) {
          updateUsageStats(msg.usage_stats);
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
      case 'session_ended':
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

      case 'usage_stats':
        updateUsageStats(msg.stats);
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

      case 'permission_request':
        if (msg.request) {
          pendingPermissions[msg.request.request_id] = msg.request;
          if (prefs['pref-sound']) playAlertSound();
          if (prefs['pref-vibrate']) triggerVibration();
          renderDashboard();
        }
        break;

      case 'permission_resolved':
        if (msg.request_id) {
          delete pendingPermissions[msg.request_id];
          renderDashboard();
        }
        break;

      case 'dispatcher_prefs_updated':
        if (msg.dispatcher_prefs) {
          var dp = msg.dispatcher_prefs;
          Object.keys(DISPATCHER_PREF_MAP).forEach(function (prefId) {
            var apiKey = DISPATCHER_PREF_MAP[prefId];
            if (apiKey in dp) {
              savePref(prefId, dp[apiKey]);
            }
          });
          syncPrefsToDOM();
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

    // TTS narration
    if (window.AttentionTTS) {
      window.AttentionTTS.handleEvent(msg);
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

    // Render pending permission tiles — only for sessions NOT already
    // visible (a visible waiting session already has its own action buttons).
    var visibleSessionIds = {};
    for (var vi = 0; vi < visible.length; vi++) {
      visibleSessionIds[visible[vi].session_id] = true;
    }
    var permIds = Object.keys(pendingPermissions);
    for (var pi = 0; pi < permIds.length; pi++) {
      var perm = pendingPermissions[permIds[pi]];
      if (!visibleSessionIds[perm.session_id]) {
        elContent.appendChild(createPermissionTile(perm));
      }
    }

    var groupBy = prefs['pref-group-by'] || 'none';

    if (groupBy !== 'none') {
      // Group sessions by project or machine
      var groups = {};
      var groupOrder = [];
      for (var gi = 0; gi < visible.length; gi++) {
        var groupKey = groupBy === 'project'
          ? (shortProject(visible[gi].project) || 'unknown')
          : (visible[gi].machine || 'unknown');
        if (!groups[groupKey]) {
          groups[groupKey] = [];
          groupOrder.push(groupKey);
        }
        groups[groupKey].push(visible[gi]);
      }
      for (var go = 0; go < groupOrder.length; go++) {
        var gk = groupOrder[go];
        var groupEl = document.createElement('div');
        groupEl.className = 'tile-group';
        var labelEl = document.createElement('div');
        labelEl.className = 'tile-group-label';
        labelEl.textContent = gk;
        groupEl.appendChild(labelEl);
        var groupGrid = document.createElement('div');
        groupGrid.className = 'tile-grid';
        var members = groups[gk];
        for (var gm = 0; gm < members.length; gm++) {
          var tileEl = createTile(members[gm]);
          if (inputValues[members[gm].session_id]) {
            var inp = tileEl.querySelector('.tile-text-input');
            if (inp) inp.value = inputValues[members[gm].session_id];
          }
          groupGrid.appendChild(tileEl);
        }
        groupEl.appendChild(groupGrid);
        elContent.appendChild(groupEl);
      }
    } else {
      for (var i = 0; i < visible.length; i++) {
        var tileEl = createTile(visible[i]);
        if (inputValues[visible[i].session_id]) {
          var inp = tileEl.querySelector('.tile-text-input');
          if (inp) inp.value = inputValues[visible[i].session_id];
        }
        elContent.appendChild(tileEl);
      }
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
    var hasControl = !!s.tmux_session || !!s.pty_port;
    var sessionName = sessionDisplayName(s);
    var age = (s.idle_seconds != null && prefs['pref-idle'] !== false) ? formatAge(s.idle_seconds) : '';

    var tile = document.createElement('div');
    var isStale = s.idle_seconds != null && s.idle_seconds > 1800; // >30 min
    var isSkill = (s.project || '').indexOf('skill:') === 0;
    var isSubagent = !s.tmux_session && !s.pty_port && !isSkill;
    var cls = 'tile state-' + state;
    if (isStale) cls += ' tile-stale';
    if (isSkill || isSubagent) cls += ' tile-background';
    tile.className = cls;
    tile.dataset.sessionId = s.session_id;

    var html = '';
    if (state === 'waiting' && prompt) {
      html = buildWaitingTile(s, prompt, sessionName, age, hasControl);
    } else {
      html = buildIdleTile(s, sessionName, age, state);
    }

    tile.innerHTML = html;
    wireTileActions(tile, s);

    return tile;
  }

  function createPermissionTile(perm) {
    var tile = document.createElement('div');
    tile.className = 'tile tile--permission';
    tile.dataset.requestId = perm.request_id;

    var toolPreview = formatToolInput(perm.tool_name, perm.tool_input);
    var projectName = shortProject(perm.project) || perm.machine || '?';

    var html = '';
    html += '<div class="tile-header">';
    html += '<span class="tile-name">' + esc(projectName) + '</span>';
    html += '<span class="badge-tool badge-tool--permission">' + esc(perm.tool_name) + '</span>';
    html += '</div>';

    html += '<div class="tile-meta">';
    html += '<span>@' + esc(perm.machine || '?') + '</span>';
    html += '</div>';

    if (toolPreview) {
      html += '<div class="tile-code">' + esc(toolPreview.length > 120 ? toolPreview.substring(0, 117) + '\u2026' : toolPreview) + '</div>';
    }

    html += '<div class="tile-question">\ud83d\udd12 Allow this tool?</div>';

    html += '<div class="tile-actions">';
    html += '<button class="btn btn-sm btn-allow" data-action="perm-allow" data-rid="' + esc(perm.request_id) + '">Allow</button>';
    html += '<button class="btn btn-sm btn-deny" data-action="perm-deny" data-rid="' + esc(perm.request_id) + '">Deny</button>';
    html += '</div>';

    tile.innerHTML = html;

    // Wire button events
    tile.querySelector('[data-action="perm-allow"]').addEventListener('click', function() {
      sendPermissionDecision(perm.request_id, 'allow');
    });
    tile.querySelector('[data-action="perm-deny"]').addEventListener('click', function() {
      sendPermissionDecision(perm.request_id, 'deny');
    });

    return tile;
  }

  function buildWaitingTile(s, prompt, sessionName, age, hasControl) {
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
    } else if (!hasControl) {
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

    // Action buttons (for sessions with tmux or PTY control)
    if (hasControl) {
      html += buildTileActions(s, prompt);
    }

    // Context bar (if stats available)
    if (sessionContextStats[s.session_id]) {
      html += renderContextBarHTML(s.session_id);
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

    // Context bar (if stats available)
    if (sessionContextStats[s.session_id]) {
      html += renderContextBarHTML(s.session_id);
    }

    return html;
  }

  // ---- Tile Event Wiring ----

  function wireTileActions(tile, session) {
    tile.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-action]');
      if (!btn) {
        // Click on tile body: open terminal slide if tmux or PTY available
        if (!e.target.closest('input') && (session.tmux_session || session.pty_port)) {
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

    // TTS settings init
    if (window.AttentionTTS) {
      var ttsS = window.AttentionTTS.getSettings();
      var el;
      el = document.getElementById('pref-tts-enabled'); if (el) el.checked = ttsS.enabled;
      el = document.getElementById('pref-tts-verbosity'); if (el) el.value = ttsS.verbosity;
      el = document.getElementById('pref-tts-volume'); if (el) el.value = ttsS.volume;
      el = document.getElementById('pref-tts-cooldown'); if (el) el.value = ttsS.cooldown;
      el = document.getElementById('pref-tts-cat-attention'); if (el) el.checked = ttsS.categories.attention;
      el = document.getElementById('pref-tts-cat-permission'); if (el) el.checked = ttsS.categories.permission;
      el = document.getElementById('pref-tts-cat-milestone'); if (el) el.checked = ttsS.categories.milestone;
      el = document.getElementById('pref-tts-cat-difficulty'); if (el) el.checked = ttsS.categories.difficulty;
      el = document.getElementById('pref-tts-cat-lifecycle'); if (el) el.checked = ttsS.categories.lifecycle;
      el = document.getElementById('pref-tts-cat-didactic'); if (el) el.checked = ttsS.categories.didactic;
    }

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

    // Push TTS prefs to hub on startup (sync localStorage → hub)
    pushTTSPrefsToHub();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // ---- TTS Settings Listeners ----

  function pushTTSPrefsToHub() {
    if (!window.AttentionTTS) return;
    var s = window.AttentionTTS.getSettings();
    fetch('/api/attention/tts-prefs', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: s.enabled, categories: s.categories })
    }).catch(function () {});
  }

  function bindTTSPref(id, fn) {
    var el = document.getElementById(id);
    if (el) el.addEventListener('change', function () {
      fn(el.type === 'checkbox' ? el.checked : el.value);
      pushTTSPrefsToHub();
    });
  }
  if (window.AttentionTTS) {
    bindTTSPref('pref-tts-enabled', function (v) { window.AttentionTTS.updateSettings({ enabled: v }); });
    bindTTSPref('pref-tts-verbosity', function (v) { window.AttentionTTS.updateSettings({ verbosity: v }); });
    bindTTSPref('pref-tts-volume', function (v) { window.AttentionTTS.updateSettings({ volume: parseFloat(v) }); });
    bindTTSPref('pref-tts-cooldown', function (v) { window.AttentionTTS.updateSettings({ cooldown: parseInt(v, 10) }); });
    bindTTSPref('pref-tts-cat-attention', function (v) { window.AttentionTTS.updateSettings({ categories: { attention: v } }); });
    bindTTSPref('pref-tts-cat-permission', function (v) { window.AttentionTTS.updateSettings({ categories: { permission: v } }); });
    bindTTSPref('pref-tts-cat-milestone', function (v) { window.AttentionTTS.updateSettings({ categories: { milestone: v } }); });
    bindTTSPref('pref-tts-cat-difficulty', function (v) { window.AttentionTTS.updateSettings({ categories: { difficulty: v } }); });
    bindTTSPref('pref-tts-cat-lifecycle', function (v) { window.AttentionTTS.updateSettings({ categories: { lifecycle: v } }); });
    bindTTSPref('pref-tts-cat-didactic', function (v) { window.AttentionTTS.updateSettings({ categories: { didactic: v } }); });
  }

  // ---- Dispatcher Preferences Sync ----

  var DISPATCHER_PREF_MAP = {
    'pref-conversation-active': 'conversation_active',
    'pref-show-agent-exchanges': 'show_agent_exchanges',
    'pref-voice-response': 'voice_response',
    'pref-auto-print-pos': 'auto_print_pos',
    'pref-hear-agents': 'hear_agents'
  };

  function pushDispatcherPref(prefId) {
    var apiKey = DISPATCHER_PREF_MAP[prefId];
    if (!apiKey) return;
    var body = {};
    body[apiKey] = prefs[prefId];
    fetch('/api/attention/dispatcher-prefs', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).catch(function () {});
  }

  // Bind dispatcher pref toggles
  Object.keys(DISPATCHER_PREF_MAP).forEach(function (prefId) {
    var el = document.getElementById(prefId);
    if (el) {
      el.addEventListener('change', function () {
        savePref(prefId, el.checked);
        pushDispatcherPref(prefId);
      });
    }
  });

  // Load dispatcher prefs from hub on startup
  fetch('/api/attention/dispatcher-prefs')
    .then(function (r) { return r.json(); })
    .then(function (dp) {
      Object.keys(DISPATCHER_PREF_MAP).forEach(function (prefId) {
        var apiKey = DISPATCHER_PREF_MAP[prefId];
        if (apiKey in dp) {
          savePref(prefId, dp[apiKey]);
        }
      });
      syncPrefsToDOM();
    })
    .catch(function () {});

  // ---- AudioContext Unlock for TTS ----

  document.addEventListener('click', function ttsTap() {
    if (window.AttentionTTS) window.AttentionTTS.unlockAudio();
    document.removeEventListener('click', ttsTap);
  }, { once: true });

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
