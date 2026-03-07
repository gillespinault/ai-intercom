/* ================================================================
   TTS Narrator — Voice announcements for Attention Hub PWA
   Queues announcements, fetches PCM from /api/attention/tts,
   plays via AudioContext.
   ================================================================ */
(function () {
  'use strict';

  var DEFAULTS = {
    enabled: true,
    volume: 0.8,
    verbosity: 'informatif',
    cooldown: 5,
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
      return JSON.parse(JSON.stringify(DEFAULTS));
    }
  }

  function saveSettings(s) {
    localStorage.setItem('tts_settings', JSON.stringify(s));
  }

  var settings = loadSettings();

  var queue = [];
  var playing = false;
  var lastPlayTime = 0;
  var recentMessages = {};

  function enqueue(text, category, priority) {
    if (!settings.enabled) return;
    if (!settings.categories[category]) return;

    var now = Date.now();
    if (recentMessages[text] && now - recentMessages[text] < 10000) return;
    recentMessages[text] = now;

    Object.keys(recentMessages).forEach(function (k) {
      if (now - recentMessages[k] > 15000) delete recentMessages[k];
    });

    queue.push({ text: text, category: category, priority: priority, time: now });

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

  var audioCtx = null;

  function getAudioCtx() {
    if (!audioCtx) {
      try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { /* noop */ }
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

  window.AttentionTTS = {
    handleEvent: function (event) {
      if (!settings.enabled) return;

      var type = event.type;
      var session = event.session;

      if (type === 'tts_announce') {
        enqueue(event.message, event.category || 'milestone', event.priority || 'normal');
        return;
      }

      if (!session) return;
      var project = session.project || 'session';

      if (type === 'state_changed' && session.state === 'waiting') {
        var prompt = session.prompt || {};
        var ptype = prompt.type || 'text_input';
        if (ptype === 'permission') {
          enqueue(generateText(project, 'waiting_permission', { tool: prompt.tool }), 'permission', 'normal');
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

    getSettings: function () { return JSON.parse(JSON.stringify(settings)); },

    updateSettings: function (updates) {
      if (updates.categories) {
        Object.assign(settings.categories, updates.categories);
        delete updates.categories;
      }
      Object.assign(settings, updates);
      saveSettings(settings);
    },

    unlockAudio: function () {
      var ctx = getAudioCtx();
      if (ctx && ctx.state === 'suspended') ctx.resume();
    },
  };
})();
