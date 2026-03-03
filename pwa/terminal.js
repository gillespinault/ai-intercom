/* ================================================================
   Attention Hub — Terminal Panel Manager
   xterm.js instances, content polling, prompt overlays,
   non-tmux detection, enhanced ANSI color theme.
   ================================================================ */

(function () {
  'use strict';

  // ---- State ----

  /** Map of session_id -> { term, container, pollTimer, lastContent } */
  var terminals = {};

  var POLL_INTERVAL = 3000;

  // ---- Helpers ----

  var hub = function () { return window.AttentionHub || {}; };
  var esc = function (s) { return hub().esc ? hub().esc(s) : String(s || ''); };
  var shortProject = function (p) { return hub().shortProject ? hub().shortProject(p) : String(p || ''); };

  // ---- Enhanced ANSI Theme ----

  var XTERM_THEME = {
    background:       '#060a12',
    foreground:       '#e8ecf1',
    cursor:           '#e94560',
    cursorAccent:     '#060a12',
    selectionBackground: '#e9456035',
    selectionForeground: '#ffffff',

    // Normal colors
    black:   '#1a2238',
    red:     '#e94560',
    green:   '#00d4aa',
    yellow:  '#ffb347',
    blue:    '#4a7dff',
    magenta: '#c77dff',
    cyan:    '#00bcd4',
    white:   '#a8b2c1',

    // Bright variants
    brightBlack:   '#4a5168',
    brightRed:     '#ff6b81',
    brightGreen:   '#2effc7',
    brightYellow:  '#ffd37d',
    brightBlue:    '#7da5ff',
    brightMagenta: '#dda0ff',
    brightCyan:    '#40e0d0',
    brightWhite:   '#f0f4f8'
  };

  // ---- Terminal Instance Management ----

  function getOrCreateTerminal(sessionId) {
    if (terminals[sessionId]) return terminals[sessionId];

    var state = {
      term: null,
      container: null,
      pollTimer: null,
      lastContent: '',
      sessionId: sessionId
    };

    terminals[sessionId] = state;
    return state;
  }

  function initXterm(termState, bodyEl) {
    if (typeof Terminal === 'undefined') {
      bodyEl.innerHTML = '<div style="color:var(--text-muted);padding:16px;font-size:12px;font-family:inherit;">Loading terminal...</div>';
      return;
    }

    var term = new Terminal({
      theme: XTERM_THEME,
      fontSize: 12,
      fontFamily: "'IBM Plex Mono', 'Menlo', 'Consolas', monospace",
      fontWeight: 400,
      fontWeightBold: 600,
      cursorBlink: false,
      cursorStyle: 'block',
      scrollback: 2000,
      convertEol: true,
      disableStdin: false,
      allowProposedApi: true,
      lineHeight: 1.2
    });

    term.open(bodyEl);
    termState.term = term;

    // Forward keyboard input
    term.onData(function (data) {
      if (hub().wsSend) {
        hub().wsSend({
          action: 'respond',
          session_id: termState.sessionId,
          keys: data
        });
      }
    });

    // Auto-resize
    try {
      var resizeObserver = new ResizeObserver(function () {
        if (term && bodyEl.clientWidth > 0) {
          var charWidth = 7.2;
          var cols = Math.floor((bodyEl.clientWidth - 12) / charWidth);
          var rows = Math.floor((bodyEl.clientHeight - 6) / 15.6);
          if (cols > 10 && rows > 3) {
            term.resize(Math.min(cols, 200), Math.min(rows, 60));
          }
        }
      });
      resizeObserver.observe(bodyEl);
      termState._resizeObserver = resizeObserver;
    } catch (e) { /* ResizeObserver unavailable */ }
  }

  function fetchTerminalContent(termState) {
    var url = '/api/attention/terminal/' + encodeURIComponent(termState.sessionId);
    fetch(url)
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (data) {
        var content = data.content || '';
        if (content !== termState.lastContent && termState.term) {
          termState.term.clear();
          termState.term.write(content);
          termState.lastContent = content;
          var prefs = hub().prefs ? hub().prefs() : {};
          if (prefs['pref-autoscroll'] !== false) termState.term.scrollToBottom();
        }
      })
      .catch(function () { /* silently ignore */ });
  }

  function startPolling(termState) {
    if (termState.pollTimer) return;
    fetchTerminalContent(termState);
    termState.pollTimer = setInterval(function () {
      fetchTerminalContent(termState);
    }, POLL_INTERVAL);
  }

  function stopPolling(termState) {
    if (termState.pollTimer) {
      clearInterval(termState.pollTimer);
      termState.pollTimer = null;
    }
  }

  function destroyTerminal(sessionId) {
    var termState = terminals[sessionId];
    if (!termState) return;
    stopPolling(termState);
    if (termState._resizeObserver) termState._resizeObserver.disconnect();
    if (termState.term) termState.term.dispose();
    delete terminals[sessionId];
  }

  // ---- Prompt Overlay ----

  function createPromptOverlay(panelEl, session) {
    var prompt = session.prompt;
    if (!prompt) return;

    var overlay = document.createElement('div');
    overlay.className = 'terminal-prompt-overlay visible';

    var inner = document.createElement('div');
    inner.className = 'terminal-prompt-inner';

    var html = '';

    switch (prompt.type) {
      case 'permission':
        var toolName = prompt.tool || 'Unknown tool';
        var preview = prompt.command_preview || '';
        html += '<div class="overlay-prompt-text">';
        html += '<span class="overlay-tool-name">' + esc(toolName) + '</span>';
        if (preview) {
          html += '<pre class="overlay-command">' + esc(preview.length > 120 ? preview.substring(0, 117) + '...' : preview) + '</pre>';
        }
        html += '</div><div class="overlay-actions">';
        var choices = prompt.choices || [
          { key: 'y', label: 'Allow' }, { key: 'n', label: 'Deny' }, { key: 'a', label: 'Always' }
        ];
        for (var i = 0; i < choices.length; i++) {
          var c = choices[i];
          var cls = c.key === 'y' ? 'btn-allow' : (c.key === 'n' ? 'btn-deny' : 'btn-secondary');
          html += '<button class="btn ' + cls + '" data-overlay-key="' + esc(c.key) + '">' + esc(c.label) + '</button>';
        }
        html += '</div>';
        break;

      case 'question':
        var question = prompt.question || prompt.raw_text || 'Question';
        html += '<div class="overlay-prompt-text">' + esc(question) + '</div>';
        html += '<div class="overlay-actions">';
        var qc = prompt.choices || [];
        for (var j = 0; j < qc.length; j++) {
          html += '<button class="btn btn-secondary" data-overlay-key="' + esc(qc[j].key) + '">' + esc(qc[j].label) + '</button>';
        }
        html += '</div>';
        break;

      case 'text_input':
        var rawText = prompt.raw_text || '';
        var contextLines = rawText.split('\n').slice(-3).join('\n');
        html += '<div class="overlay-prompt-text"><pre class="overlay-context">' + esc(contextLines) + '</pre></div>';
        html += '<div class="overlay-input-row">';
        html += '<input type="text" class="input-text overlay-text-input" placeholder="Type response...">';
        html += '<button class="btn btn-primary" data-overlay-action="send-text">Send</button>';
        html += '</div>';
        break;
    }

    inner.innerHTML = html;
    overlay.appendChild(inner);

    // Wire events
    overlay.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-overlay-key]');
      if (btn) {
        if (hub().sendRespond) hub().sendRespond(session.session_id, btn.dataset.overlayKey);
        overlay.classList.remove('visible');
        setTimeout(function () { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }, 300);
        return;
      }

      var sendBtn = e.target.closest('[data-overlay-action="send-text"]');
      if (sendBtn) {
        var input = overlay.querySelector('.overlay-text-input');
        if (input && input.value.trim()) {
          if (hub().sendRespond) hub().sendRespond(session.session_id, input.value.trim() + '\n');
          overlay.classList.remove('visible');
          setTimeout(function () { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }, 300);
        }
        return;
      }

      if (e.target === overlay) {
        overlay.classList.remove('visible');
        setTimeout(function () { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }, 300);
      }
    });

    var textInput = inner.querySelector('.overlay-text-input');
    if (textInput) {
      textInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          var value = textInput.value.trim();
          if (value && hub().sendRespond) {
            hub().sendRespond(session.session_id, value + '\n');
            overlay.classList.remove('visible');
            setTimeout(function () { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }, 300);
          }
        }
      });
    }

    // Touch swipe down to dismiss
    var startY = 0;
    overlay.addEventListener('touchstart', function (e) { startY = e.touches[0].clientY; }, { passive: true });
    overlay.addEventListener('touchend', function (e) {
      if (e.changedTouches[0].clientY - startY > 60) {
        overlay.classList.remove('visible');
        setTimeout(function () { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }, 300);
      }
    }, { passive: true });

    panelEl.appendChild(overlay);
  }

  // ---- Build Terminal Panel ----

  function buildTerminalPanel(session) {
    var state = (session.state || 'idle').toLowerCase();
    var sessionName = session.session_name || shortProject(session.project) || 'Session';
    var hasTmux = !!session.tmux_session;

    var panel = document.createElement('div');
    panel.className = 'terminal-panel';
    panel.dataset.sessionId = session.session_id;

    // Header
    var header = document.createElement('div');
    header.className = 'terminal-panel-header';
    header.innerHTML =
      '<span class="terminal-panel-machine">' + esc(session.machine || '') + '</span>' +
      '<span class="terminal-panel-title">' + esc(sessionName) + '</span>' +
      (!hasTmux ? '<span class="badge-monitoring" style="margin-left:auto;">monitor only</span>' : '') +
      '<span class="card-state-badge ' + state + '" style="margin-left:' + (hasTmux ? 'auto' : 'var(--sp-2)') + ';">' + esc(state) + '</span>';
    panel.appendChild(header);

    // Body
    var body = document.createElement('div');
    body.className = 'terminal-body';
    panel.appendChild(body);

    if (hasTmux) {
      // Initialize xterm.js for tmux sessions
      var termState = getOrCreateTerminal(session.session_id);
      termState.container = body;

      requestAnimationFrame(function () {
        if (body.clientWidth > 0) {
          initXterm(termState, body);
          startPolling(termState);
        } else {
          setTimeout(function () {
            initXterm(termState, body);
            startPolling(termState);
          }, 100);
        }
      });

      // Prompt overlay for waiting tmux sessions
      if (state === 'waiting' && session.prompt) {
        requestAnimationFrame(function () { createPromptOverlay(panel, session); });
      }
    } else {
      // Non-tmux: show monitoring-only placeholder
      body.innerHTML =
        '<div class="terminal-no-tmux">' +
          '<div class="terminal-no-tmux-icon">\u25C9</div>' +
          '<div>No tmux session attached</div>' +
          '<div style="opacity:0.5;">Terminal view unavailable</div>' +
        '</div>';
    }

    return panel;
  }

  // ---- Public API ----

  function renderTerminals(container, sessions) {
    cleanupStaleTerminals(sessions);
    sessions.forEach(function (s) {
      destroyTerminal(s.session_id);
      container.appendChild(buildTerminalPanel(s));
    });
  }

  function renderSingleTerminal(container, session) {
    destroyTerminal(session.session_id);
    container.appendChild(buildTerminalPanel(session));
  }

  function beforeRender() {
    var ids = Object.keys(terminals);
    for (var i = 0; i < ids.length; i++) {
      var ts = terminals[ids[i]];
      stopPolling(ts);
      if (ts._resizeObserver) { ts._resizeObserver.disconnect(); ts._resizeObserver = null; }
      if (ts.term) { ts.term.dispose(); ts.term = null; }
    }
    terminals = {};
  }

  function onModeChange(fromMode, toMode) {
    // beforeRender handles cleanup
  }

  function cleanupStaleTerminals(currentSessions) {
    var activeIds = {};
    for (var i = 0; i < currentSessions.length; i++) activeIds[currentSessions[i].session_id] = true;
    var ids = Object.keys(terminals);
    for (var j = 0; j < ids.length; j++) {
      if (!activeIds[ids[j]]) destroyTerminal(ids[j]);
    }
  }

  // ---- Expose ----

  window.TerminalManager = {
    renderTerminals: renderTerminals,
    renderSingleTerminal: renderSingleTerminal,
    beforeRender: beforeRender,
    onModeChange: onModeChange,
    destroyTerminal: destroyTerminal
  };

})();
