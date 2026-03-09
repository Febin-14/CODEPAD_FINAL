(function() {
    var currentUsername = null;
    var chatWs = null;
    var unreadCount = 0;
    var currentMode = 'team'; // 'team' | 'ai'
    var teamMessages = [];
    var aiMessages = [];
    var mediaRecorder = null;
    var recordingStream = null;
    var recordingChunks = [];

    function getCookie(name) {
        var match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
        return match ? decodeURIComponent(match[2]) : null;
    }

    function formatChatTime(iso) {
        if (!iso) return '';
        try {
            var d = new Date(iso);
            var now = new Date();
            var sameDay = d.toDateString() === now.toDateString();
            if (sameDay) {
                return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            }
            return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        } catch (e) {
            return '';
        }
    }

    function escapeHtml(s) {
        if (!s) return '';
        var div = document.createElement('div');
        div.textContent = s;
        return div.innerHTML;
    }

    function appendMessageToContainer(data, isSelf) {
        var container = document.getElementById('chatMessages');
        if (!container) return;
        var div = document.createElement('div');
        div.className = 'chat-msg ' + (isSelf ? 'msg-self' : 'msg-other');
        var senderLabel = data.sender_username || '';
        div.innerHTML = '<div class="chat-msg-sender">' + escapeHtml(senderLabel) + '</div>' +
            '<div class="chat-msg-text">' + escapeHtml(data.message || '') + '</div>' +
            '<div class="chat-msg-time">' + formatChatTime(data.created_at) + '</div>';
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
    }

    function appendMessage(data, isSelf) {
        appendMessageToContainer(data, isSelf);
    }

    function renderMessages() {
        var container = document.getElementById('chatMessages');
        if (!container) return;
        container.innerHTML = '';
        var list = currentMode === 'team' ? teamMessages : aiMessages;
        list.forEach(function(m) {
            var isSelf = m.sender_username === currentUsername;
            appendMessageToContainer(m, isSelf);
        });
        container.scrollTop = container.scrollHeight;
    }

    function setStatus(text) {
        var el = document.getElementById('chatStatus');
        if (el) el.textContent = text;
    }

    function setMode(mode) {
        currentMode = mode;
        var teamBtn = document.getElementById('chatModeTeam');
        var aiBtn = document.getElementById('chatModeAi');
        if (teamBtn) teamBtn.classList.toggle('chat-mode-active', mode === 'team');
        if (aiBtn) aiBtn.classList.toggle('chat-mode-active', mode === 'ai');
        var title = document.getElementById('chatPanelTitle');
        if (title) title.textContent = mode === 'team' ? 'Team Chat' : 'AI Assistant';
        var placeholder = document.getElementById('chatInput');
        if (placeholder) placeholder.placeholder = mode === 'team' ? 'Type a message…' : 'Ask me anything…';
        var voiceWrap = document.getElementById('chatVoiceWrap');
        if (voiceWrap) voiceWrap.style.display = mode === 'ai' ? '' : 'none';
        if (mode === 'team' && mediaRecorder && mediaRecorder.state === 'recording') stopRecording();
        renderMessages();
    }

    function connectChat() {
        currentUsername = getCookie('username');
        if (!currentUsername) return;

        var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        var wsUrl = protocol + '//' + window.location.host + '/ws/chat';
        chatWs = new WebSocket(wsUrl);

        chatWs.onopen = function() {
            if (currentMode === 'team') setStatus('Connected · Group chat');
        };
        chatWs.onmessage = function(event) {
            var data;
            try {
                data = JSON.parse(event.data);
            } catch (e) { return; }
            if (data.type === 'message') {
                teamMessages.push(data);
                if (currentMode === 'team') {
                    var isSelf = data.sender_username === currentUsername;
                    appendMessage(data, isSelf);
                }
                var wrap = document.getElementById('chatWidgetWrap');
                if (wrap && !wrap.classList.contains('chat-open')) {
                    unreadCount++;
                    var badge = document.getElementById('chatUnreadBadge');
                    if (badge) {
                        badge.textContent = unreadCount > 99 ? '99+' : unreadCount;
                        document.getElementById('chatToggleBtn').classList.add('has-badge');
                    }
                }
            }
        };
        chatWs.onclose = function() {
            if (currentMode === 'team') setStatus('Disconnected. Reconnecting…');
            setTimeout(connectChat, 3000);
        };
        chatWs.onerror = function() {
            if (currentMode === 'team') setStatus('Connection error');
        };
    }

    function loadTeamHistory() {
        fetch('/api/chat/messages?limit=80')
            .then(function(r) { return r.ok ? r.json() : Promise.reject(); })
            .then(function(data) {
                teamMessages = data.messages || [];
                if (currentMode === 'team') renderMessages();
            })
            .catch(function() {
                teamMessages = [];
                if (currentMode === 'team') renderMessages();
            });
    }

    function openChat() {
        var wrap = document.getElementById('chatWidgetWrap');
        if (wrap) wrap.classList.add('chat-open');
        unreadCount = 0;
        var badge = document.getElementById('chatUnreadBadge');
        if (badge) badge.textContent = '0';
        document.getElementById('chatToggleBtn').classList.remove('has-badge');
        if (currentMode === 'team') {
            loadTeamHistory();
            setStatus(chatWs && chatWs.readyState === WebSocket.OPEN ? 'Connected · Group chat' : 'Connecting…');
        } else {
            setStatus('AI Assistant · GPT-4o');
            renderMessages();
        }
    }

    function closeChat() {
        var wrap = document.getElementById('chatWidgetWrap');
        if (wrap) wrap.classList.remove('chat-open');
    }

    function sendMessage() {
        var input = document.getElementById('chatInput');
        if (!input) return;
        var text = (input.value || '').trim();
        if (!text) return;

        if (currentMode === 'team') {
            if (!chatWs || chatWs.readyState !== WebSocket.OPEN) return;
            chatWs.send(JSON.stringify({ type: 'message', text: text }));
            input.value = '';
            return;
        }

        // AI mode
        input.value = '';
        var now = new Date().toISOString();
        var userMsg = { sender_username: currentUsername, sender_role: 'user', message: text, created_at: now };
        aiMessages.push(userMsg);
        renderMessages();

        var typingId = 'ai-typing-' + Date.now();
        var container = document.getElementById('chatMessages');
        var typingDiv = document.createElement('div');
        typingDiv.id = typingId;
        typingDiv.className = 'chat-msg msg-other chat-msg-typing';
        typingDiv.innerHTML = '<div class="chat-msg-sender">AI</div><div class="chat-msg-text">Thinking…</div>';
        container.appendChild(typingDiv);
        container.scrollTop = container.scrollHeight;

        fetch('/api/chat/ai', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: text })
        })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var el = document.getElementById(typingId);
                if (el) el.remove();
                var reply = (data.reply || 'No response.').trim();
                var aiMsg = { sender_username: 'AI', sender_role: 'assistant', message: reply, created_at: new Date().toISOString() };
                aiMessages.push(aiMsg);
                renderMessages();
            })
            .catch(function(err) {
                var el = document.getElementById(typingId);
                if (el) el.remove();
                var aiMsg = { sender_username: 'AI', sender_role: 'assistant', message: 'Sorry, something went wrong. Please try again.', created_at: new Date().toISOString() };
                aiMessages.push(aiMsg);
                renderMessages();
            });
    }

    function stopRecording() {
        if (mediaRecorder && mediaRecorder.state === 'recording') {
            mediaRecorder.stop();
        }
        if (recordingStream) {
            recordingStream.getTracks().forEach(function(t) { t.stop(); });
            recordingStream = null;
        }
        mediaRecorder = null;
        var btn = document.getElementById('chatVoiceBtn');
        if (btn) { btn.classList.remove('chat-voice-recording'); btn.title = 'Record voice message'; }
    }

    function startRecording() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            alert('Microphone access is not supported in this browser.');
            return;
        }
        recordingChunks = [];
        navigator.mediaDevices.getUserMedia({ audio: true })
            .then(function(stream) {
                recordingStream = stream;
                var mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : 'audio/webm';
                try {
                    mediaRecorder = new MediaRecorder(stream);
                } catch (e) {
                    stream.getTracks().forEach(function(t) { t.stop(); });
                    alert('Recording not supported: ' + e.message);
                    return;
                }
                mediaRecorder.ondataavailable = function(e) { if (e.data.size) recordingChunks.push(e.data); };
                mediaRecorder.onstop = function() {
                    stream.getTracks().forEach(function(t) { t.stop(); });
                    recordingStream = null;
                    var btn = document.getElementById('chatVoiceBtn');
                    if (btn) { btn.classList.remove('chat-voice-recording'); btn.title = 'Record voice message'; }
                    if (recordingChunks.length === 0) return;
                    var blob = new Blob(recordingChunks, { type: mime });
                    sendVoiceToAi(blob);
                };
                mediaRecorder.start(200);
                var btn = document.getElementById('chatVoiceBtn');
                if (btn) { btn.classList.add('chat-voice-recording'); btn.title = 'Stop recording'; }
            })
            .catch(function(err) {
                alert('Microphone access denied or failed: ' + (err.message || 'Unknown error'));
            });
    }

    function toggleVoiceRecording() {
        if (currentMode !== 'ai') return;
        var btn = document.getElementById('chatVoiceBtn');
        if (mediaRecorder && mediaRecorder.state === 'recording') {
            stopRecording();
            return;
        }
        startRecording();
    }

    function sendVoiceToAi(audioBlob) {
        var now = new Date().toISOString();
        var typingId = 'ai-typing-' + Date.now();
        var container = document.getElementById('chatMessages');
        var typingDiv = document.createElement('div');
        typingDiv.id = typingId;
        typingDiv.className = 'chat-msg msg-other chat-msg-typing';
        typingDiv.innerHTML = '<div class="chat-msg-sender">AI</div><div class="chat-msg-text">Listening & thinking…</div>';
        container.appendChild(typingDiv);
        container.scrollTop = container.scrollHeight;

        var fd = new FormData();
        fd.append('audio', audioBlob, 'voice.webm');
        fetch('/api/chat/ai/voice', { method: 'POST', body: fd })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var el = document.getElementById(typingId);
                if (el) el.remove();
                var transcript = (data.transcript || '').trim();
                var reply = (data.reply || '').trim();
                if (transcript) {
                    var userMsg = { sender_username: currentUsername, sender_role: 'user', message: transcript, created_at: now };
                    aiMessages.push(userMsg);
                }
                if (reply) {
                    var aiMsg = { sender_username: 'AI', sender_role: 'assistant', message: reply, created_at: new Date().toISOString() };
                    aiMessages.push(aiMsg);
                }
                renderMessages();
            })
            .catch(function(err) {
                var el = document.getElementById(typingId);
                if (el) el.remove();
                var aiMsg = { sender_username: 'AI', sender_role: 'assistant', message: 'Voice request failed. Please try again or type your message.', created_at: new Date().toISOString() };
                aiMessages.push(aiMsg);
                renderMessages();
            });
    }

    function initChatWidget() {
        currentUsername = getCookie('username');
        if (!currentUsername) return;

        var toggleBtn = document.getElementById('chatToggleBtn');
        var closeBtn = document.getElementById('chatPanelClose');
        var sendBtn = document.getElementById('chatSendBtn');
        var input = document.getElementById('chatInput');
        var modeTeam = document.getElementById('chatModeTeam');
        var modeAi = document.getElementById('chatModeAi');

        if (toggleBtn) toggleBtn.addEventListener('click', function() {
            var wrap = document.getElementById('chatWidgetWrap');
            if (wrap && wrap.classList.contains('chat-open')) closeChat();
            else openChat();
        });
        if (closeBtn) closeBtn.addEventListener('click', closeChat);
        if (sendBtn) sendBtn.addEventListener('click', sendMessage);
        if (input) {
            input.addEventListener('keydown', function(e) {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    sendMessage();
                }
            });
        }
        if (modeTeam) modeTeam.addEventListener('click', function() { setMode('team'); setStatus(chatWs && chatWs.readyState === WebSocket.OPEN ? 'Connected · Group chat' : 'Connecting…'); });
        if (modeAi) modeAi.addEventListener('click', function() { setMode('ai'); setStatus('AI Assistant · GPT-4o'); renderMessages(); });
        var voiceBtn = document.getElementById('chatVoiceBtn');
        if (voiceBtn) voiceBtn.addEventListener('click', toggleVoiceRecording);

        setMode('team');
        var voiceWrap = document.getElementById('chatVoiceWrap');
        if (voiceWrap) voiceWrap.style.display = 'none';
        connectChat();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initChatWidget);
    } else {
        initChatWidget();
    }
})();
