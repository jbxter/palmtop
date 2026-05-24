/* Chat widget — talks to /api/chat via SSE-over-POST */
(function () {
  'use strict';

  var toggle = document.getElementById('chat-toggle');
  var panel = document.getElementById('chat-panel');
  var closeBtn = document.getElementById('chat-close');
  var messages = document.getElementById('chat-messages');
  var input = document.getElementById('chat-input');
  var sendBtn = document.getElementById('chat-send');

  // Generate or retrieve session ID (UUID v4)
  function uuidv4() {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  var sessionId = sessionStorage.getItem('pa_session');
  if (!sessionId) {
    sessionId = uuidv4();
    sessionStorage.setItem('pa_session', sessionId);
  }

  var sending = false;

  function isMobile() {
    return window.innerWidth <= 600;
  }

  // Open chat
  function openChat() {
    panel.classList.add('open');
    if (isMobile()) {
      toggle.classList.add('hidden');
      document.body.style.overflow = 'hidden'; // prevent background scroll
    }
    input.focus();
  }

  // Close chat
  function closeChat() {
    panel.classList.remove('open');
    toggle.classList.remove('hidden');
    document.body.style.overflow = '';
  }

  toggle.addEventListener('click', function () {
    if (panel.classList.contains('open')) {
      closeChat();
    } else {
      openChat();
    }
  });

  closeBtn.addEventListener('click', closeChat);

  // Handle mobile keyboard: resize messages area so input stays visible
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', function () {
      if (!panel.classList.contains('open') || !isMobile()) return;
      var keyboardHeight = window.innerHeight - window.visualViewport.height;
      if (keyboardHeight > 50) {
        panel.style.height = window.visualViewport.height + 'px';
      } else {
        panel.style.height = '';
      }
      scrollToBottom();
    });
  }

  // Send message
  function send() {
    var text = input.value.trim();
    if (!text || sending) return;

    appendMsg('user', text);
    input.value = '';
    sending = true;
    sendBtn.disabled = true;

    var thinkEl = appendMsg('assistant thinking', '...');

    fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, session_id: sessionId }),
    })
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(function (err) {
            throw new Error(err.error || 'Request failed');
          });
        }

        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';
        var reply = '';

        function read() {
          return reader.read().then(function (result) {
            if (result.done) {
              if (thinkEl.parentNode) thinkEl.remove();
              if (reply) appendMsg('assistant', reply);
              sending = false;
              sendBtn.disabled = false;
              return;
            }

            buffer += decoder.decode(result.value, { stream: true });

            var parts = buffer.split('\n\n');
            buffer = parts.pop();

            for (var i = 0; i < parts.length; i++) {
              var chunk = parts[i];
              var event = 'message';
              var data = '';
              var lines = chunk.split('\n');
              for (var j = 0; j < lines.length; j++) {
                var line = lines[j];
                if (line.startsWith('event: ')) event = line.slice(7);
                else if (line.startsWith('data: ')) {
                  try { data = JSON.parse(line.slice(6)); }
                  catch (e) { data = line.slice(6); }
                }
              }

              if (event === 'done' && data) {
                reply = data;
              } else if (event === 'error') {
                reply = data || 'Something went wrong.';
              }
            }

            return read();
          });
        }

        return read();
      })
      .catch(function (err) {
        if (thinkEl.parentNode) thinkEl.remove();
        appendMsg('assistant', err.message || 'Connection error. Try again.');
        sending = false;
        sendBtn.disabled = false;
      });
  }

  sendBtn.addEventListener('click', send);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') send();
  });

  function scrollToBottom() {
    messages.scrollTop = messages.scrollHeight;
  }

  function appendMsg(cls, text) {
    var el = document.createElement('div');
    el.className = 'chat-msg ' + cls;
    el.textContent = text;
    messages.appendChild(el);
    scrollToBottom();
    return el;
  }
})();
