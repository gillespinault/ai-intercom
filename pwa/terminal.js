/* ================================================================
   Attention Hub -- Terminal Panel Manager
   Manages xterm.js instances, content polling, prompt overlays,
   and keyboard input forwarding.
   ================================================================ */

(function () {
  'use strict';

  // ---- State ----

  /** Map of session_id -> { term, container, pollTimer, lastContent } */
  var terminals = {};

  /** Poll interval in milliseconds */
  var POLL_INTERVAL = 3000;

  // ---- Helpers ----

  var hub = function () { return window.AttentionHub || {}; };
  var esc = function (s) { return hub().esc ? hub().esc(s) : String(s || ''); };
  var shortProject = function (p) { return hub().shortProject ? hub().shortProject(p) : String(p || ''); };
  var formatIdle = function (s) { return hub().formatIdle ? hub().formatIdle(s) : String(s || ''); };

  // ---- Terminal Instance Management ----

  /**
   * Create or retrieve a terminal instance for a session.
   * @param {string} sessionId
   * @returns {Object} terminal state object
   */
  function getOrCreateTerminal(sessionId) {
    if (terminals[sessionId]) {
      return terminals[sessionId];
    }

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

  /**
   * Initialize an xterm.js instance inside a DOM container.
   */
  function initXterm(termState, bodyEl) {
    if (typeof Terminal === 'undefined') {
      bodyEl.innerHTML = '<div style="color:var(--text-muted);padding:16px;font-size:13px;">Loading terminal library...</div>';
      return;
    }

    var term = new Terminal({
      theme: {
        background: '#0c0c1a',
        foreground: '#eee',
        cursor: '#e94560',
        selectionBackground: '#e9456044',
        black: '#0c0c1a',
        red: '#e94560',
        green: '#4caf50',
        yellow: '#ff9800',
        blue: '#0f3460',
        magenta: '#9c27b0',
        cyan: '#00bcd4',
        white: '#eee'
      },
      fontSize: 13,
      fontFamily: "'Fira Code', 'Cascadia Code', 'JetBrains Mono', monospace",
      cursorBlink: false,
      cursorStyle: 'block',
      scrollback: 2000,
      convertEol: true,
      disableStdin: false,
      allowProposedApi: true
    });

    term.open(bodyEl);
    termState.term = term;

    // Forward keyboard input to session via wsSend
    term.onData(function (data) {
      if (hub().wsSend) {
        hub().wsSend({
          action: 'respond',
          session_id: termState.sessionId,
          keys: data
        });
      }
    });

    // Auto-resize on container size change
    try {
      var resizeObserver = new ResizeObserver(function () {
        // Fit terminal to container -- basic column calculation
        if (term && bodyEl.clientWidth > 0) {
          var charWidth = 7.8; // approximate for 13px monospace
          var cols = Math.floor((bodyEl.clientWidth - 16) / charWidth);
          var rows = Math.floor((bodyEl.clientHeight - 8) / 17); // line height ~17px
          if (cols > 10 && rows > 3) {
            term.resize(Math.min(cols, 200), Math.min(rows, 60));
          }
        }
      });
      resizeObserver.observe(bodyEl);
      termState._resizeObserver = resizeObserver;
    } catch (e) {
      // ResizeObserver not available, skip
    }
  }

  /**
   * Fetch terminal content from API and update xterm.
   */
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

          // Auto-scroll if preference enabled
          var prefs = hub().prefs ? hub().prefs() : {};
          if (prefs['pref-autoscroll'] !== false) {
            termState.term.scrollToBottom();
          }
        }
      })
      .catch(function () {
        // Silently ignore fetch errors (session may have ended)
      });
  }

  /**
   * Start polling for terminal content.
   */
  function startPolling(termState) {
    if (termState.pollTimer) return;
    // Initial fetch
    fetchTerminalContent(termState);
    // Poll every POLL_INTERVAL
    termState.pollTimer = setInterval(function () {
      fetchTerminalContent(termState);
    }, POLL_INTERVAL);
  }

  /**
   * Stop polling for a terminal.
   */
  function stopPolling(termState) {
    if (termState.pollTimer) {
      clearInterval(termState.pollTimer);
      termState.pollTimer = null;
    }
  }

  /**
   * Destroy a terminal instance.
   */
  function destroyTerminal(sessionId) {
    var termState = terminals[sessionId];
    if (!termState) return;

    stopPolling(termState);

    if (termState._resizeObserver) {
      termState._resizeObserver.disconnect();
    }

    if (termState.term) {
      termState.term.dispose();
    }

    delete terminals[sessionId];
  }

  // ---- Prompt Overlay ----

  /**
   * Create a prompt overlay on top of a terminal panel.
   * @param {HTMLElement} panelEl - the .terminal-panel element
   * @param {Object} session - session with prompt data
   */
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
        html += '</div>';
        html += '<div class="overlay-actions">';
        var choices = prompt.choices || [
          { key: 'y', label: 'Allow' },
          { key: 'n', label: 'Deny' },
          { key: 'a', label: 'Always allow' }
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
        var qchoices = prompt.choices || [];
        for (var j = 0; j < qchoices.length; j++) {
          html += '<button class="btn btn-secondary" data-overlay-key="' + esc(qchoices[j].key) + '">' + esc(qchoices[j].label) + '</button>';
        }
        html += '</div>';
        break;

      case 'text_input':
        var rawText = prompt.raw_text || '';
        var contextLines = rawText.split('\n').slice(-3).join('\n');
        html += '<div class="overlay-prompt-text"><pre class="overlay-context">' + esc(contextLines) + '</pre></div>';
        html += '<div class="overlay-input-row">';
        html += '<input type="text" class="input-text overlay-text-input" placeholder="Type your response...">';
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
        var key = btn.dataset.overlayKey;
        if (hub().sendRespond) {
          hub().sendRespond(session.session_id, key);
        }
        overlay.classList.remove('visible');
        setTimeout(function () { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }, 300);
        return;
      }

      var sendBtn = e.target.closest('[data-overlay-action="send-text"]');
      if (sendBtn) {
        var input = overlay.querySelector('.overlay-text-input');
        if (input && input.value.trim()) {
          if (hub().sendRespond) {
            hub().sendRespond(session.session_id, input.value.trim() + '\n');
          }
          overlay.classList.remove('visible');
          setTimeout(function () { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }, 300);
        }
        return;
      }

      // Dismiss overlay on clicking backdrop
      if (e.target === overlay) {
        overlay.classList.remove('visible');
        setTimeout(function () { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }, 300);
      }
    });

    // Enter key on text input
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
    overlay.addEventListener('touchstart', function (e) {
      startY = e.touches[0].clientY;
    }, { passive: true });
    overlay.addEventListener('touchend', function (e) {
      var endY = e.changedTouches[0].clientY;
      if (endY - startY > 60) {
        overlay.classList.remove('visible');
        setTimeout(function () { if (overlay.parentNode) overlay.parentNode.removeChild(overlay); }, 300);
      }
    }, { passive: true });

    panelEl.appendChild(overlay);
  }

  // ---- Build Terminal Panel ----

  /**
   * Build a full terminal panel DOM element for a session.
   * @param {Object} session
   * @returns {HTMLElement}
   */
  function buildTerminalPanel(session) {
    var state = (session.state || 'idle').toLowerCase();
    var sessionName = session.session_name || shortProject(session.project) || 'Session';

    var panel = document.createElement('div');
    panel.className = 'terminal-panel';
    panel.dataset.sessionId = session.session_id;

    // Header
    var header = document.createElement('div');
    header.className = 'terminal-panel-header';
    header.innerHTML =
      '<span class="terminal-panel-machine">' + esc(session.machine || '') + '</span>' +
      '<span class="terminal-panel-title">' + esc(sessionName) + '</span>' +
      '<span class="card-state-badge ' + state + '" style="margin-left:auto;">' + esc(state) + '</span>';
    panel.appendChild(header);

    // Body (terminal will be mounted here)
    var body = document.createElement('div');
    body.className = 'terminal-body';
    panel.appendChild(body);

    // Initialize xterm.js
    var termState = getOrCreateTerminal(session.session_id);
    termState.container = body;

    // Wait a tick for DOM insertion, then init xterm
    requestAnimationFrame(function () {
      if (body.clientWidth > 0) {
        initXterm(termState, body);
        startPolling(termState);
      } else {
        // Retry after a short delay (DOM may not be ready)
        setTimeout(function () {
          initXterm(termState, body);
          startPolling(termState);
        }, 100);
      }
    });

    // Add prompt overlay if session is waiting
    if (state === 'waiting' && session.prompt) {
      requestAnimationFrame(function () {
        createPromptOverlay(panel, session);
      });
    }

    return panel;
  }

  // ---- Public API (called by app.js) ----

  /**
   * Render all terminals in the terminals view.
   */
  function renderTerminals(container, sessions) {
    // Destroy all terminals that no longer exist
    cleanupStaleTerminals(sessions);

    sessions.forEach(function (s) {
      // Destroy existing terminal for this session (will be re-created)
      destroyTerminal(s.session_id);
      var panel = buildTerminalPanel(s);
      container.appendChild(panel);
    });
  }

  /**
   * Render a single terminal in a container (used by split mode).
   */
  function renderSingleTerminal(container, session) {
    // Destroy existing terminal for this session
    destroyTerminal(session.session_id);
    var panel = buildTerminalPanel(session);
    container.appendChild(panel);
  }

  /**
   * Called before renderCurrentMode clears the DOM.
   * Stops polling but keeps terminal state for potential reuse.
   */
  function beforeRender() {
    var ids = Object.keys(terminals);
    for (var i = 0; i < ids.length; i++) {
      var ts = terminals[ids[i]];
      stopPolling(ts);
      // Detach from DOM but don't destroy term yet
      if (ts._resizeObserver) {
        ts._resizeObserver.disconnect();
        ts._resizeObserver = null;
      }
      if (ts.term) {
        ts.term.dispose();
        ts.term = null;
      }
    }
    // Clear all terminal state since DOM will be wiped
    terminals = {};
  }

  /**
   * Notification of mode change.
   */
  function onModeChange(fromMode, toMode) {
    // Nothing special needed; beforeRender handles cleanup
  }

  /**
   * Remove terminals for sessions that no longer exist.
   */
  function cleanupStaleTerminals(currentSessions) {
    var activeIds = {};
    for (var i = 0; i < currentSessions.length; i++) {
      activeIds[currentSessions[i].session_id] = true;
    }
    var ids = Object.keys(terminals);
    for (var j = 0; j < ids.length; j++) {
      if (!activeIds[ids[j]]) {
        destroyTerminal(ids[j]);
      }
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
