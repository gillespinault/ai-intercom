/* ================================================================
   Attention Hub -- Main Application Module
   WebSocket connection, session state, mode switching, action cards,
   split view, and preferences system.
   ================================================================ */

(function () {
  'use strict';

  // ---- State ----

  /** @type {WebSocket|null} */
  var ws = null;

  /** @type {'connected'|'reconnecting'|'disconnected'} */
  var connState = 'disconnected';

  /** Reconnect bookkeeping */
  var reconnectTimer = null;
  var reconnectDelay = 1000;
  var RECONNECT_MAX = 30000;

  /** @type {Array<Object>} */
  var sessions = [];

  /** @type {'actions'|'terminals'|'split'} */
  var currentMode = 'actions';

  /** Whether preferences panel is open */
  var prefsOpen = false;

  /** Currently selected session in split view */
  var splitSelectedId = null;

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
    'pref-idle': true
  };

  /** @type {Object<string, boolean>} */
  var prefs = {};

  function loadPrefs() {
    var keys = Object.keys(PREF_DEFAULTS);
    for (var i = 0; i < keys.length; i++) {
      var key = keys[i];
      var stored = localStorage.getItem('ah-' + key);
      if (stored !== null) {
        prefs[key] = stored === 'true';
      } else {
        prefs[key] = PREF_DEFAULTS[key];
      }
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
      if (el && el.type === 'checkbox') {
        el.checked = prefs[keys[i]];
      }
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

  // ---- DOM refs (populated on init) ----

  var elContent = null;
  var elConnDot = null;
  var elModeTabs = null;
  var elPrefsBackdrop = null;
  var elPrefsPanel = null;

  // ---- Helpers ----

  /**
   * Format an idle duration in seconds to a human-readable string.
   * @param {number} seconds
   * @returns {string}
   */
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

  /** Minimal HTML escaping. */
  function esc(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /** Extract short project name from path. */
  function shortProject(path) {
    if (!path) return '';
    var parts = path.replace(/\/+$/, '').split('/');
    return parts[parts.length - 1] || path;
  }

  /** State sort priority (lower = higher priority). */
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
      // Within same state, sort by idle_seconds descending
      var ia = a.idle_seconds || 0;
      var ib = b.idle_seconds || 0;
      return ib - ia;
    });
  }

  function filterSessions(list) {
    return list.filter(function (s) {
      var state = (s.state || '').toLowerCase();
      var prompt = s.prompt;

      // Filter by state
      if (state === 'working' && !prefs['pref-working']) return false;
      if (state === 'thinking' && !prefs['pref-thinking']) return false;

      // Filter by prompt type (only for waiting sessions)
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

  // ---- WebSocket Connection Manager ----

  function wsUrl() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + location.host + '/api/attention/ws';
  }

  function setConnState(state) {
    connState = state;
    if (elConnDot) {
      elConnDot.className = 'conn-dot ' + state;
    }
  }

  function connectWS() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

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
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
    };

    ws.onclose = function () {
      ws = null;
      setConnState('disconnected');
      scheduleReconnect();
    };

    ws.onerror = function () {
      // onclose will fire after this
    };

    ws.onmessage = function (event) {
      try {
        var msg = JSON.parse(event.data);
      } catch (e) {
        return;
      }
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

  /**
   * Send a JSON message through the WebSocket.
   * @param {Object} msg
   */
  function wsSend(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }

  /**
   * Send a respond action for a session.
   * @param {string} sessionId
   * @param {string} keys
   */
  function sendRespond(sessionId, keys) {
    wsSend({ action: 'respond', session_id: sessionId, keys: keys });
  }

  // ---- WebSocket Message Handling ----

  function handleWSMessage(msg) {
    switch (msg.type) {
      case 'snapshot':
        sessions = msg.sessions || [];
        renderCurrentMode();
        break;

      case 'session_update':
        upsertSession(msg.session);
        renderCurrentMode();
        break;

      case 'session_end':
        removeSession(msg.session_id || (msg.session && msg.session.session_id));
        renderCurrentMode();
        break;

      default:
        if (msg.session) {
          upsertSession(msg.session);
          renderCurrentMode();
        }
        break;
    }
  }

  function upsertSession(sessionData) {
    if (!sessionData || !sessionData.session_id) return;
    var idx = sessions.findIndex(function (s) {
      return s.session_id === sessionData.session_id;
    });
    if (idx >= 0) {
      sessions[idx] = sessionData;
    } else {
      sessions.push(sessionData);
    }
  }

  function removeSession(sessionId) {
    if (!sessionId) return;
    sessions = sessions.filter(function (s) {
      return s.session_id !== sessionId;
    });
    // Clean up split selection
    if (splitSelectedId === sessionId) {
      splitSelectedId = null;
    }
  }

  // ---- Mode Switching ----

  function setMode(mode) {
    if (mode === currentMode) return;

    // Notify terminal manager of mode change
    if (window.TerminalManager) {
      window.TerminalManager.onModeChange(currentMode, mode);
    }

    currentMode = mode;

    if (elModeTabs) {
      var tabs = elModeTabs.querySelectorAll('.mode-tab');
      tabs.forEach(function (tab) {
        tab.setAttribute('aria-selected', tab.dataset.mode === mode ? 'true' : 'false');
      });
    }

    renderCurrentMode();
  }

  function renderCurrentMode() {
    if (!elContent) return;

    // Notify terminal manager before clearing DOM
    if (window.TerminalManager) {
      window.TerminalManager.beforeRender();
    }

    elContent.innerHTML = '';
    elContent.className = 'main-content view-' + currentMode;

    var visible = getVisibleSessions();

    if (visible.length === 0 && sessions.length === 0) {
      renderEmpty();
      return;
    }

    if (visible.length === 0) {
      renderFilteredEmpty();
      return;
    }

    switch (currentMode) {
      case 'actions':
        renderActions(visible);
        break;
      case 'terminals':
        renderTerminals(visible);
        break;
      case 'split':
        renderSplit(visible);
        break;
    }
  }

  function renderEmpty() {
    var div = document.createElement('div');
    div.className = 'empty-state';
    div.innerHTML =
      '<div class="empty-state-icon" aria-hidden="true">&#x1f4e1;</div>' +
      '<div class="empty-state-text">No active sessions. Waiting for agents to connect.</div>';
    elContent.appendChild(div);
  }

  function renderFilteredEmpty() {
    var div = document.createElement('div');
    div.className = 'empty-state';
    div.innerHTML =
      '<div class="empty-state-icon" aria-hidden="true">&#x1f50d;</div>' +
      '<div class="empty-state-text">' + sessions.length + ' session(s) hidden by filters. Adjust preferences to see them.</div>';
    elContent.appendChild(div);
  }

  // ================================================================
  // Task 11: Actions Mode
  // ================================================================

  function renderActions(visible) {
    visible.forEach(function (s) {
      var card = createActionCard(s, false);
      elContent.appendChild(card);
    });
  }

  /**
   * Create a full action card for a session.
   * @param {Object} s - session object
   * @param {boolean} compact - if true, render compact version for split left panel
   * @returns {HTMLElement}
   */
  function createActionCard(s, compact) {
    var state = (s.state || 'idle').toLowerCase();
    var prompt = s.prompt;
    var card = document.createElement('div');
    card.className = 'session-card state-' + state + (compact ? ' session-card-compact' : '');
    card.dataset.sessionId = s.session_id;

    var idleText = '';
    if (s.idle_seconds != null && prefs['pref-idle'] !== false) {
      idleText = formatIdle(s.idle_seconds);
    }

    // Session name
    var sessionName = s.session_name || shortProject(s.project) || 'Session';

    // Header
    var headerHtml =
      '<div class="card-header">' +
        '<span class="card-session-name">' + esc(sessionName) + '</span>' +
        '<span class="card-machine-tag">' + esc(s.machine || 'unknown') + '</span>' +
        '<span class="card-state-badge ' + state + '">' + esc(state) + '</span>' +
      '</div>';

    // Body: depends on state + prompt type
    var bodyHtml = '';
    var actionsHtml = '';

    if (state === 'waiting' && prompt) {
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
    } else {
      // Working or thinking: minimal info
      var toolInfo = s.last_tool ? 'Tool: ' + s.last_tool : '';
      var projectInfo = shortProject(s.project);
      bodyHtml =
        '<div class="card-body card-body-minimal">' +
          (projectInfo ? '<span class="card-info-item">' + esc(projectInfo) + '</span>' : '') +
          (toolInfo ? '<span class="card-info-item">' + esc(toolInfo) + '</span>' : '') +
        '</div>';
    }

    // Footer
    var footerHtml =
      '<div class="card-footer">' +
        '<span class="card-idle">' + (idleText ? 'idle ' + idleText : '') + '</span>' +
      '</div>';

    card.innerHTML = headerHtml + bodyHtml + (compact ? '' : actionsHtml) + footerHtml;

    // Wire up action button events (after innerHTML is set)
    if (!compact) {
      wireCardActions(card, s);
    }

    return card;
  }

  // ---- Permission cards ----

  function renderPermissionBody(prompt, compact) {
    var toolName = prompt.tool || 'Unknown tool';
    var preview = prompt.command_preview || '';
    if (compact && preview.length > 60) {
      preview = preview.substring(0, 57) + '...';
    }
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
    if (compact && question.length > 80) {
      question = question.substring(0, 77) + '...';
    }
    return '<div class="card-body">' + esc(question) + '</div>';
  }

  function renderQuestionActions(sessionId, prompt) {
    var choices = prompt.choices || [];
    var html = '<div class="card-actions">';
    for (var i = 0; i < choices.length; i++) {
      var c = choices[i];
      html += '<button class="btn btn-secondary" data-action="respond" data-key="' + esc(c.key) + '">' + esc(c.label) + '</button>';
    }
    // Always add Other... button for free text
    html += '<button class="btn btn-ghost btn-other" data-action="show-input">Other...</button>';
    html += '</div>';
    // Hidden input row for "Other..."
    html += '<div class="card-input-row hidden">' +
      '<input type="text" class="input-text card-free-input" placeholder="Type your response...">' +
      '<button class="btn btn-primary btn-send-free" data-action="send-free">Send</button>' +
      '</div>';
    return html;
  }

  // ---- Text input cards ----

  function renderTextInputBody(prompt, compact) {
    var rawText = prompt.raw_text || '';
    // Show last few lines of context
    var lines = rawText.split('\n');
    var contextLines = lines.slice(-4).join('\n');
    if (compact && contextLines.length > 100) {
      contextLines = contextLines.substring(0, 97) + '...';
    }
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

  // ---- Wire action button events ----

  function wireCardActions(card, session) {
    // Respond buttons (Allow, Deny, Always, choice buttons)
    card.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-action]');
      if (!btn) return;

      var action = btn.dataset.action;

      if (action === 'respond') {
        var key = btn.dataset.key;
        if (key) {
          sendRespond(session.session_id, key);
          // Visual feedback: disable buttons
          var buttons = card.querySelectorAll('[data-action="respond"]');
          buttons.forEach(function (b) { b.disabled = true; b.classList.add('btn-sent'); });
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

    // Enter key on text inputs
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
      if (sendBtn) {
        sendBtn.disabled = true;
        sendBtn.textContent = 'Sent';
      }
    });
  }

  // ================================================================
  // Task 12: Terminal Mode (delegates to TerminalManager)
  // ================================================================

  function renderTerminals(visible) {
    if (window.TerminalManager) {
      window.TerminalManager.renderTerminals(elContent, visible);
    } else {
      // Fallback if terminal.js hasn't loaded
      visible.forEach(function (s) {
        var panel = document.createElement('div');
        panel.className = 'terminal-panel';
        panel.innerHTML =
          '<div class="terminal-panel-header">' +
            '<span class="terminal-panel-title">' + esc(s.machine || '') + ' / ' + esc(shortProject(s.project)) + '</span>' +
          '</div>' +
          '<div class="terminal-body" style="min-height:200px;color:var(--text-muted);padding:16px;font-size:13px;">' +
            'Loading terminal manager...' +
          '</div>';
        elContent.appendChild(panel);
      });
    }
  }

  // ================================================================
  // Task 13: Split Mode
  // ================================================================

  function renderSplit(visible) {
    // Left panel: compact cards
    var left = document.createElement('div');
    left.className = 'split-left';

    // Right panel: terminal
    var right = document.createElement('div');
    right.className = 'split-right';

    // Auto-select first if nothing selected
    if (!splitSelectedId || !visible.find(function (s) { return s.session_id === splitSelectedId; })) {
      splitSelectedId = visible[0] ? visible[0].session_id : null;
    }

    visible.forEach(function (s) {
      var card = createActionCard(s, true);
      if (s.session_id === splitSelectedId) {
        card.classList.add('session-card-selected');
      }
      card.style.cursor = 'pointer';
      card.addEventListener('click', function () {
        splitSelectedId = s.session_id;
        // Update card selection visuals
        var allCards = left.querySelectorAll('.session-card');
        allCards.forEach(function (c) { c.classList.remove('session-card-selected'); });
        card.classList.add('session-card-selected');
        // Re-render right panel terminal
        renderSplitTerminal(right, s);
      });
      left.appendChild(card);
    });

    elContent.appendChild(left);
    elContent.appendChild(right);

    // Render terminal for selected session
    var selected = visible.find(function (s) { return s.session_id === splitSelectedId; });
    if (selected) {
      renderSplitTerminal(right, selected);
    } else {
      right.innerHTML =
        '<div class="split-empty">' +
          '<span>Select a session to view terminal</span>' +
        '</div>';
    }
  }

  function renderSplitTerminal(container, session) {
    container.innerHTML = '';
    if (window.TerminalManager) {
      window.TerminalManager.renderSingleTerminal(container, session);
    } else {
      container.innerHTML =
        '<div class="terminal-panel">' +
          '<div class="terminal-panel-header">' +
            '<span class="terminal-panel-title">' + esc(session.machine || '') + ' / ' + esc(shortProject(session.project)) + '</span>' +
          '</div>' +
          '<div class="terminal-body" style="min-height:300px;color:var(--text-muted);padding:16px;font-size:13px;">' +
            'Loading terminal manager...' +
          '</div>' +
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

  // ---- Service Worker Registration ----

  function registerSW() {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker
        .register('/attention/sw.js', { scope: '/attention' })
        .catch(function (err) {
          console.warn('SW registration failed:', err);
        });
    }
  }

  // ---- Init ----

  function init() {
    // Load preferences from localStorage
    loadPrefs();

    // Cache DOM refs
    elContent = document.getElementById('content');
    elConnDot = document.getElementById('conn-dot');
    elModeTabs = document.getElementById('mode-tabs');
    elPrefsBackdrop = document.getElementById('prefs-backdrop');
    elPrefsPanel = document.getElementById('prefs-panel');

    // Sync prefs to DOM checkboxes
    syncPrefsToDOM();

    // Wire pref toggle change listeners
    initPrefsListeners();

    // Mode tab clicks
    if (elModeTabs) {
      elModeTabs.addEventListener('click', function (e) {
        var tab = e.target.closest('.mode-tab');
        if (tab && tab.dataset.mode) {
          setMode(tab.dataset.mode);
        }
      });
    }

    // Settings button
    var btnSettings = document.getElementById('btn-settings');
    if (btnSettings) {
      btnSettings.addEventListener('click', togglePrefs);
    }

    // Close prefs on backdrop click
    if (elPrefsBackdrop) {
      elPrefsBackdrop.addEventListener('click', closePrefs);
    }

    // Initial render
    renderCurrentMode();

    // Connect WebSocket
    connectWS();

    // Register service worker
    registerSW();
  }

  // ---- Entry Point ----

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Expose public API
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
    createActionCard: createActionCard
  };

})();
