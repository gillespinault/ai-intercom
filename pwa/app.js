/* ================================================================
   Attention Hub — Main Application Module
   WebSocket, session state, machine grouping, sound alerts,
   toast notifications, event timeline, tmux-centric UX.
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

  /** @type {'actions'|'terminals'|'split'} */
  var currentMode = 'actions';

  var prefsOpen = false;
  var splitSelectedId = null;

  /** Per-session event timeline: { sessionId: [{state, timestamp}] } */
  var sessionTimelines = {};

  /** Track previously known waiting sessions (for sound/vibration alerts) */
  var previousWaitingIds = {};

  // ---- Audio Context (lazy init) ----

  var audioCtx = null;

  function getAudioCtx() {
    if (!audioCtx) {
      try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { /* no audio */ }
    }
    return audioCtx;
  }

  /**
   * Play a two-tone alert: short high note then lower sustained note.
   */
  function playAlertSound() {
    var ctx = getAudioCtx();
    if (!ctx) return;

    // Resume if suspended (autoplay policy)
    if (ctx.state === 'suspended') ctx.resume();

    var now = ctx.currentTime;

    // First tone: 880Hz, 100ms
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

    // Second tone: 660Hz, 200ms (starts 120ms later)
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

  /**
   * Trigger vibration pattern if supported and enabled.
   */
  function triggerVibration() {
    if (navigator.vibrate) {
      navigator.vibrate([200, 100, 200]);
    }
  }

  // ---- Toast Notifications ----

  var TOAST_DURATION = 4000;

  /**
   * Show a toast notification.
   * @param {string} message
   * @param {'success'|'warning'|'error'|'info'} variant
   */
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

    // Auto-dismiss
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
    'pref-group-machine': true
  };

  /** @type {Object<string, boolean>} */
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
  }

  function initPrefsListeners() {
    var keys = Object.keys(PREF_DEFAULTS);
    for (var i = 0; i < keys.length; i++) {
      (function (key) {
        var el = document.getElementById(key);
        if (el) {
          el.addEventListener('change', function () {
            savePref(key, el.checked);
            renderCurrentMode();
          });
        }
      })(keys[i]);
    }
  }

  // ---- DOM refs ----

  var elContent = null;
  var elConnDot = null;
  var elModeTabs = null;
  var elPrefsBackdrop = null;
  var elPrefsPanel = null;
  var elHeaderStats = null;

  // ---- Helpers ----

  function formatIdle(seconds) {
    if (seconds == null || seconds < 0) return '';
    if (seconds < 60) return seconds + 's';
    var m = Math.floor(seconds / 60);
    var s = seconds % 60;
    if (m < 60) return s ? m + 'm' + String(s).padStart(2, '0') + 's' : m + 'm';
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

  function statePriority(state) {
    switch ((state || '').toLowerCase()) {
      case 'waiting':  return 0;
      case 'thinking': return 1;
      case 'working':  return 2;
      default:         return 3;
    }
  }

  // ---- Session Sorting & Filtering ----

  function sortSessions(list) {
    return list.slice().sort(function (a, b) {
      var pa = statePriority(a.state);
      var pb = statePriority(b.state);
      if (pa !== pb) return pa - pb;
      return (b.idle_seconds || 0) - (a.idle_seconds || 0);
    });
  }

  function filterSessions(list) {
    return list.filter(function (s) {
      var state = (s.state || '').toLowerCase();
      var prompt = s.prompt;
      if (state === 'working' && !prefs['pref-working']) return false;
      if (state === 'thinking' && !prefs['pref-thinking']) return false;
      if (state === 'waiting' && prompt) {
        var ptype = prompt.type || '';
        if (ptype === 'permission' && !prefs['pref-permission']) return false;
        if (ptype === 'question' && !prefs['pref-question']) return false;
        if (ptype === 'text_input' && !prefs['pref-text-input']) return false;
      }
      return true;
    });
  }

  function getVisibleSessions() {
    return sortSessions(filterSessions(sessions));
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
    // Only record if state changed
    if (timeline.length > 0 && timeline[timeline.length - 1].state === state) return;
    timeline.push({ state: state, timestamp: Date.now() });
    // Keep last 10
    if (timeline.length > 10) timeline.shift();
  }

  function getTimeline(sessionId) {
    return sessionTimelines[sessionId] || [];
  }

  // ---- Alert Logic ----

  /**
   * Check for newly waiting sessions and trigger alerts.
   */
  function checkAlerts() {
    var currentWaitingIds = {};
    for (var i = 0; i < sessions.length; i++) {
      if ((sessions[i].state || '').toLowerCase() === 'waiting') {
        currentWaitingIds[sessions[i].session_id] = true;
      }
    }

    // Find new waiting sessions
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

    // Toast on state changes
    if (prev !== state) {
      if (state === 'connected' && prev !== 'connected') {
        showToast('Connected to hub', 'success');
      } else if (state === 'disconnected' && prev === 'connected') {
        showToast('Connection lost — reconnecting...', 'warning');
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
    showToast('Response sent', 'success');
  }

  // ---- WS Message Handling ----

  function handleWSMessage(msg) {
    switch (msg.type) {
      case 'snapshot':
        sessions = msg.sessions || [];
        // Record initial timeline state for all sessions
        for (var i = 0; i < sessions.length; i++) {
          recordTimelineEvent(sessions[i].session_id, (sessions[i].state || '').toLowerCase());
        }
        checkAlerts();
        updateHeaderStats();
        renderCurrentMode();
        break;

      case 'session_update':
        upsertSession(msg.session);
        checkAlerts();
        updateHeaderStats();
        renderCurrentMode();
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
        renderCurrentMode();
        break;

      default:
        if (msg.session) {
          upsertSession(msg.session);
          checkAlerts();
          updateHeaderStats();
          renderCurrentMode();
        }
        break;
    }
  }

  function getSessionName(sessionId) {
    for (var i = 0; i < sessions.length; i++) {
      if (sessions[i].session_id === sessionId) {
        return sessions[i].session_name || shortProject(sessions[i].project);
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
      // Check if this is a new session appearing
      sessions[idx] = sessionData;
    } else {
      sessions.push(sessionData);
      showToast('New session: ' + (sessionData.session_name || shortProject(sessionData.project) || 'unknown'), 'info');
    }
  }

  function removeSession(sessionId) {
    if (!sessionId) return;
    sessions = sessions.filter(function (s) { return s.session_id !== sessionId; });
    if (splitSelectedId === sessionId) splitSelectedId = null;
  }

  // ---- Mode Switching ----

  function setMode(mode) {
    if (mode === currentMode) return;
    if (window.TerminalManager) window.TerminalManager.onModeChange(currentMode, mode);
    currentMode = mode;
    if (elModeTabs) {
      elModeTabs.querySelectorAll('.mode-tab').forEach(function (tab) {
        tab.setAttribute('aria-selected', tab.dataset.mode === mode ? 'true' : 'false');
      });
    }
    renderCurrentMode();
  }

  function renderCurrentMode() {
    if (!elContent) return;
    if (window.TerminalManager) window.TerminalManager.beforeRender();

    elContent.innerHTML = '';
    elContent.className = 'main-content view-' + currentMode;

    var visible = getVisibleSessions();

    if (visible.length === 0 && sessions.length === 0) { renderEmpty(); return; }
    if (visible.length === 0) { renderFilteredEmpty(); return; }

    switch (currentMode) {
      case 'actions':   renderActions(visible); break;
      case 'terminals': renderTerminals(visible); break;
      case 'split':     renderSplit(visible); break;
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
      '<div class="empty-state-text">' + sessions.length + ' session(s) hidden by filters.<br>Adjust preferences to see them.</div>';
    elContent.appendChild(div);
  }

  // ================================================================
  // Actions Mode (with machine grouping)
  // ================================================================

  function renderActions(visible) {
    if (prefs['pref-group-machine']) {
      renderGroupedActions(visible);
    } else {
      visible.forEach(function (s) {
        elContent.appendChild(createActionCard(s, false));
      });
    }
  }

  function renderGroupedActions(visible) {
    // Group by machine
    var groups = {};
    var groupOrder = [];
    for (var i = 0; i < visible.length; i++) {
      var machine = visible[i].machine || 'unknown';
      if (!groups[machine]) {
        groups[machine] = [];
        groupOrder.push(machine);
      }
      groups[machine].push(visible[i]);
    }

    // Sort groups: machines with waiting sessions first
    groupOrder.sort(function (a, b) {
      var aHasWaiting = groups[a].some(function (s) { return (s.state || '').toLowerCase() === 'waiting'; });
      var bHasWaiting = groups[b].some(function (s) { return (s.state || '').toLowerCase() === 'waiting'; });
      if (aHasWaiting && !bHasWaiting) return -1;
      if (!aHasWaiting && bHasWaiting) return 1;
      return a.localeCompare(b);
    });

    for (var g = 0; g < groupOrder.length; g++) {
      var machineName = groupOrder[g];
      var machineSessions = groups[machineName];

      var groupEl = document.createElement('div');
      groupEl.className = 'machine-group';
      groupEl.dataset.machine = machineName;

      // Check collapsed state from localStorage
      var isCollapsed = localStorage.getItem('ah-group-' + machineName) === 'collapsed';
      if (isCollapsed) groupEl.classList.add('collapsed');

      // Header
      var header = document.createElement('div');
      header.className = 'machine-group-header';
      header.innerHTML =
        '<svg class="machine-group-chevron" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3,2 9,6 3,10"/></svg>' +
        '<span class="machine-group-health"></span>' +
        '<span class="machine-group-name">' + esc(machineName) + '</span>' +
        '<span class="machine-group-count">' + machineSessions.length + '</span>';

      header.addEventListener('click', function (name, el) {
        return function () {
          el.classList.toggle('collapsed');
          localStorage.setItem('ah-group-' + name,
            el.classList.contains('collapsed') ? 'collapsed' : 'expanded');
        };
      }(machineName, groupEl));

      groupEl.appendChild(header);

      // Sessions container
      var sessionsEl = document.createElement('div');
      sessionsEl.className = 'machine-group-sessions';

      for (var s = 0; s < machineSessions.length; s++) {
        sessionsEl.appendChild(createActionCard(machineSessions[s], false));
      }

      groupEl.appendChild(sessionsEl);
      elContent.appendChild(groupEl);
    }
  }

  // ================================================================
  // Action Card
  // ================================================================

  function createActionCard(s, compact) {
    var state = (s.state || 'idle').toLowerCase();
    var prompt = s.prompt;
    var hasTmux = !!s.tmux_session;

    var card = document.createElement('div');
    card.className = 'session-card state-' + state + (compact ? ' session-card-compact' : '');
    card.dataset.sessionId = s.session_id;

    var idleText = '';
    if (s.idle_seconds != null && prefs['pref-idle'] !== false) {
      idleText = formatIdle(s.idle_seconds);
    }

    var sessionName = s.session_name || shortProject(s.project) || 'Session';

    // Header
    var headerHtml =
      '<div class="card-header">' +
        '<span class="card-session-name">' + esc(sessionName) + '</span>' +
        (!prefs['pref-group-machine'] ? '<span class="card-machine-tag">' + esc(s.machine || 'unknown') + '</span>' : '') +
        (!hasTmux ? '<span class="badge-monitoring">monitor</span>' : '') +
        '<span class="card-state-badge ' + state + '">' + esc(state) + '</span>' +
      '</div>';

    // Body
    var bodyHtml = '';
    var actionsHtml = '';

    if (state === 'waiting' && prompt && hasTmux) {
      // Tmux session with prompt — show full interaction
      switch (prompt.type) {
        case 'permission':
          bodyHtml = renderPermissionBody(prompt, compact);
          actionsHtml = renderPermissionActions(s.session_id, prompt);
          break;
        case 'question':
          bodyHtml = renderQuestionBody(prompt, compact);
          actionsHtml = renderQuestionActions(s.session_id, prompt);
          break;
        case 'text_input':
          bodyHtml = renderTextInputBody(prompt, compact);
          actionsHtml = renderTextInputActions(s.session_id);
          break;
        default:
          bodyHtml = '<div class="card-body">' + esc(prompt.raw_text || 'Waiting for input') + '</div>';
          break;
      }
    } else if (state === 'waiting' && prompt && !hasTmux) {
      // Non-tmux: show prompt info but no response area
      switch (prompt.type) {
        case 'permission':
          bodyHtml = renderPermissionBody(prompt, compact);
          break;
        case 'question':
          bodyHtml = renderQuestionBody(prompt, compact);
          break;
        default:
          bodyHtml = '<div class="card-body">' + esc(prompt.raw_text || 'Waiting for input') + '</div>';
          break;
      }
      // No actions for non-tmux
    } else {
      var toolInfo = s.last_tool ? 'Tool: ' + s.last_tool : '';
      var projectInfo = shortProject(s.project);
      bodyHtml =
        '<div class="card-body card-body-minimal">' +
          (projectInfo ? '<span class="card-info-item">' + esc(projectInfo) + '</span>' : '') +
          (toolInfo ? '<span class="card-info-item">' + esc(toolInfo) + '</span>' : '') +
        '</div>';
    }

    // Timeline strip
    var timelineHtml = '';
    if (!compact) {
      var timeline = getTimeline(s.session_id);
      if (timeline.length > 1) {
        timelineHtml = '<div class="card-timeline">';
        for (var t = 0; t < timeline.length; t++) {
          if (t > 0) timelineHtml += '<span class="timeline-connector"></span>';
          timelineHtml += '<span class="timeline-dot ' + esc(timeline[t].state) + '" title="' + esc(timeline[t].state) + '"></span>';
        }
        timelineHtml += '</div>';
      }
    }

    // Footer
    var footerHtml =
      '<div class="card-footer">' +
        '<span class="card-idle">' + (idleText ? 'idle ' + idleText : '') + '</span>' +
      '</div>';

    card.innerHTML = headerHtml + bodyHtml + (compact ? '' : actionsHtml) + timelineHtml + footerHtml;

    if (!compact && hasTmux) wireCardActions(card, s);

    return card;
  }

  // ---- Permission cards ----

  function renderPermissionBody(prompt, compact) {
    var toolName = prompt.tool || 'Unknown tool';
    var preview = prompt.command_preview || '';
    if (compact && preview.length > 60) preview = preview.substring(0, 57) + '...';
    return (
      '<div class="card-body">' +
        '<div class="card-prompt-tool">' + esc(toolName) + '</div>' +
        (preview ? '<pre class="card-command-preview">' + esc(preview) + '</pre>' : '') +
      '</div>'
    );
  }

  function renderPermissionActions(sessionId, prompt) {
    var choices = prompt.choices || [
      { key: 'y', label: 'Allow' },
      { key: 'n', label: 'Deny' },
      { key: 'a', label: 'Always allow' }
    ];
    var html = '<div class="card-actions">';
    for (var i = 0; i < choices.length; i++) {
      var c = choices[i];
      var btnClass = 'btn ';
      if (c.key === 'y') btnClass += 'btn-allow';
      else if (c.key === 'n') btnClass += 'btn-deny';
      else btnClass += 'btn-secondary';
      html += '<button class="' + btnClass + '" data-action="respond" data-key="' + esc(c.key) + '">' + esc(c.label) + '</button>';
    }
    html += '</div>';
    return html;
  }

  // ---- Question cards ----

  function renderQuestionBody(prompt, compact) {
    var question = prompt.question || prompt.raw_text || 'Question';
    if (compact && question.length > 80) question = question.substring(0, 77) + '...';
    return '<div class="card-body">' + esc(question) + '</div>';
  }

  function renderQuestionActions(sessionId, prompt) {
    var choices = prompt.choices || [];
    var html = '<div class="card-actions">';
    for (var i = 0; i < choices.length; i++) {
      html += '<button class="btn btn-secondary" data-action="respond" data-key="' + esc(choices[i].key) + '">' + esc(choices[i].label) + '</button>';
    }
    html += '<button class="btn btn-ghost btn-other" data-action="show-input">Other...</button>';
    html += '</div>';
    html += '<div class="card-input-row hidden">' +
      '<input type="text" class="input-text card-free-input" placeholder="Type your response...">' +
      '<button class="btn btn-primary btn-send-free" data-action="send-free">Send</button>' +
      '</div>';
    return html;
  }

  // ---- Text input cards ----

  function renderTextInputBody(prompt, compact) {
    var rawText = prompt.raw_text || '';
    var lines = rawText.split('\n');
    var contextLines = lines.slice(-4).join('\n');
    if (compact && contextLines.length > 100) contextLines = contextLines.substring(0, 97) + '...';
    return '<div class="card-body"><pre class="card-text-context">' + esc(contextLines) + '</pre></div>';
  }

  function renderTextInputActions(sessionId) {
    return (
      '<div class="card-input-row">' +
        '<input type="text" class="input-text card-text-response" placeholder="Type your response...">' +
        '<button class="btn btn-primary btn-send-text" data-action="send-text">Send</button>' +
      '</div>'
    );
  }

  // ---- Wire card events ----

  function wireCardActions(card, session) {
    card.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-action]');
      if (!btn) return;
      var action = btn.dataset.action;

      if (action === 'respond') {
        var key = btn.dataset.key;
        if (key) {
          sendRespond(session.session_id, key);
          card.querySelectorAll('[data-action="respond"]').forEach(function (b) {
            b.disabled = true; b.classList.add('btn-sent');
          });
          btn.textContent = 'Sent';
        }
      }

      if (action === 'show-input') {
        var inputRow = card.querySelector('.card-input-row');
        if (inputRow) {
          inputRow.classList.remove('hidden');
          var input = inputRow.querySelector('.card-free-input');
          if (input) input.focus();
        }
        btn.classList.add('hidden');
      }

      if (action === 'send-free') {
        var input = card.querySelector('.card-free-input');
        if (input && input.value.trim()) {
          sendRespond(session.session_id, input.value.trim() + '\n');
          input.disabled = true;
          btn.disabled = true;
          btn.textContent = 'Sent';
        }
      }

      if (action === 'send-text') {
        var input = card.querySelector('.card-text-response');
        if (input && input.value.trim()) {
          sendRespond(session.session_id, input.value.trim() + '\n');
          input.disabled = true;
          btn.disabled = true;
          btn.textContent = 'Sent';
        }
      }
    });

    card.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      var input = e.target;
      if (!input.classList.contains('input-text')) return;
      e.preventDefault();
      var value = input.value.trim();
      if (!value) return;
      sendRespond(session.session_id, value + '\n');
      input.disabled = true;
      var sendBtn = input.parentElement.querySelector('[data-action]');
      if (sendBtn) { sendBtn.disabled = true; sendBtn.textContent = 'Sent'; }
    });
  }

  // ================================================================
  // Terminal Mode
  // ================================================================

  function renderTerminals(visible) {
    if (window.TerminalManager) {
      window.TerminalManager.renderTerminals(elContent, visible);
    } else {
      visible.forEach(function (s) {
        var panel = document.createElement('div');
        panel.className = 'terminal-panel';
        panel.innerHTML =
          '<div class="terminal-panel-header">' +
            '<span class="terminal-panel-title">' + esc(s.machine || '') + ' / ' + esc(shortProject(s.project)) + '</span>' +
          '</div>' +
          '<div class="terminal-body" style="min-height:200px;color:var(--text-muted);padding:16px;font-size:13px;">Loading...</div>';
        elContent.appendChild(panel);
      });
    }
  }

  // ================================================================
  // Split Mode
  // ================================================================

  function renderSplit(visible) {
    var left = document.createElement('div');
    left.className = 'split-left';
    var right = document.createElement('div');
    right.className = 'split-right';

    if (!splitSelectedId || !visible.find(function (s) { return s.session_id === splitSelectedId; })) {
      splitSelectedId = visible[0] ? visible[0].session_id : null;
    }

    visible.forEach(function (s) {
      var card = createActionCard(s, true);
      if (s.session_id === splitSelectedId) card.classList.add('session-card-selected');
      card.style.cursor = 'pointer';
      card.addEventListener('click', function () {
        splitSelectedId = s.session_id;
        left.querySelectorAll('.session-card').forEach(function (c) { c.classList.remove('session-card-selected'); });
        card.classList.add('session-card-selected');
        renderSplitTerminal(right, s);
      });
      left.appendChild(card);
    });

    elContent.appendChild(left);
    elContent.appendChild(right);

    var selected = visible.find(function (s) { return s.session_id === splitSelectedId; });
    if (selected) {
      renderSplitTerminal(right, selected);
    } else {
      right.innerHTML = '<div class="split-empty"><span>Select a session</span></div>';
    }
  }

  function renderSplitTerminal(container, session) {
    container.innerHTML = '';
    if (window.TerminalManager) {
      window.TerminalManager.renderSingleTerminal(container, session);
    } else {
      container.innerHTML =
        '<div class="terminal-panel">' +
          '<div class="terminal-panel-header"><span class="terminal-panel-title">' +
          esc(session.machine || '') + ' / ' + esc(shortProject(session.project)) +
          '</span></div>' +
          '<div class="terminal-body" style="min-height:300px;color:var(--text-muted);padding:16px;">Loading...</div>' +
        '</div>';
    }
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
    elModeTabs = document.getElementById('mode-tabs');
    elPrefsBackdrop = document.getElementById('prefs-backdrop');
    elPrefsPanel = document.getElementById('prefs-panel');
    elHeaderStats = document.getElementById('header-stats');

    syncPrefsToDOM();
    initPrefsListeners();

    if (elModeTabs) {
      elModeTabs.addEventListener('click', function (e) {
        var tab = e.target.closest('.mode-tab');
        if (tab && tab.dataset.mode) setMode(tab.dataset.mode);
      });
    }

    var btnSettings = document.getElementById('btn-settings');
    if (btnSettings) btnSettings.addEventListener('click', togglePrefs);
    if (elPrefsBackdrop) elPrefsBackdrop.addEventListener('click', closePrefs);

    renderCurrentMode();
    connectWS();
    registerSW();
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
    formatIdle: formatIdle,
    setMode: setMode,
    currentMode: function () { return currentMode; },
    connState: function () { return connState; },
    esc: esc,
    shortProject: shortProject,
    prefs: function () { return prefs; },
    getVisibleSessions: getVisibleSessions,
    renderCurrentMode: renderCurrentMode,
    splitSelectedId: function () { return splitSelectedId; },
    createActionCard: createActionCard,
    showToast: showToast
  };

})();
