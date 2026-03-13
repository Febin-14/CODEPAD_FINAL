(function () {
    // ─── State ───────────────────────────────────────────────────────────────
    var currentUsername = null;
    var currentRole = null;

    // One WebSocket connection for team (project)
    var teamWs = null;

    // Message stores keyed per channel
    var messageStore = { team: [] };

    // Active display channel
    var activeChannel = 'team';
    // Active mode: 'chat' | 'ai'
    var currentMode = 'chat';

    var unreadTeam = 0;

    var mediaRecorder = null;
    var recordingStream = null;
    var recordingChunks = [];

    var aiMessages = [];

    // ─── Helpers ─────────────────────────────────────────────────────────────
    function getCookie(name) {
        var match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
        return match ? decodeURIComponent(match[2]) : null;
    }

    function formatChatTime(iso) {
        if (!iso) return '';
        try {
            var d = new Date(iso);
            var now = new Date();
            if (d.toDateString() === now.toDateString())
                return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' +
                d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        } catch (e) { return ''; }
    }

    function escapeHtml(s) {
        if (!s) return '';
        var div = document.createElement('div');
        div.textContent = s;
        return div.innerHTML;
    }

    // ─── UI helpers ──────────────────────────────────────────────────────────
    function setStatus(text) {
        var el = document.getElementById('chatStatus');
        if (el) el.textContent = text;
    }

    function isChatOpen() {
        var wrap = document.getElementById('chatWidgetWrap');
        return !!(wrap && wrap.classList.contains('chat-open'));
    }

    function hasActiveProject() {
        return !!window.currentProjectId;
    }

    function teamChatStatus() {
        if (!hasActiveProject()) return 'Select a project to use team chat';
        if (teamWs && teamWs.readyState === WebSocket.OPEN) return 'Connected · Team Chat';
        if (teamWs && teamWs.readyState === WebSocket.CONNECTING) return 'Connecting...';
        return 'Connecting...';
    }

    function updateBadge() {
        var total = unreadTeam;
        var badge = document.getElementById('chatUnreadBadge');
        var btn = document.getElementById('chatToggleBtn');
        if (!badge || !btn) return;
        if (total > 0) {
            badge.textContent = total > 99 ? '99+' : total;
            btn.classList.add('has-badge');
        } else {
            badge.textContent = '0';
            btn.classList.remove('has-badge');
        }
        // Mini counter
        var tbadge = document.getElementById('chatTeamBadge');
        if (tbadge) { tbadge.textContent = unreadTeam > 0 ? unreadTeam : ''; tbadge.style.display = unreadTeam > 0 ? 'inline' : 'none'; }
    }

    function appendMsgEl(data, isSelf, container) {
        var div = document.createElement('div');
        div.className = 'chat-msg ' + (isSelf ? 'msg-self' : 'msg-other');
        var senderLabel = data.sender_username || '';
        div.innerHTML = '<div class="chat-msg-sender">' + escapeHtml(senderLabel) + '</div>' +
            '<div class="chat-msg-text">' + escapeHtml(data.message || '') + '</div>' +
            '<div class="chat-msg-time">' + formatChatTime(data.created_at) + '</div>';
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
    }

    function renderMessages() {
        var container = document.getElementById('chatMessages');
        if (!container) return;
        container.innerHTML = '';
        if (currentMode === 'ai') {
            aiMessages.forEach(function (m) {
                var isSelf = m.sender_username === currentUsername;
                appendMsgEl(m, isSelf, container);
            });
        } else {
            var list = messageStore[activeChannel] || [];
            list.forEach(function (m) {
                var isSelf = m.sender_username === currentUsername;
                appendMsgEl(m, isSelf, container);
            });
        }
        container.scrollTop = container.scrollHeight;
    }

    // ─── WebSocket connections ────────────────────────────────────────────────
    function wsUrl(projectId) {
        var proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        return proto + '//' + window.location.host + '/ws/chat?project_id=' + projectId;
    }

    function connectTeam(projectId) {
        if (!projectId) {
            setStatus('Select a project to use team chat');
            return;
        }
        if (teamWs && (teamWs.readyState === WebSocket.OPEN || teamWs.readyState === WebSocket.CONNECTING)) return;
        teamWs = new WebSocket(wsUrl(projectId));
        teamWs.onopen = function () {
            loadHistory('team', projectId);
            if (activeChannel === 'team' && currentMode === 'chat') setStatus(teamChatStatus());
        };
        teamWs.onmessage = function (ev) { handleIncoming(ev, 'team'); };
        teamWs.onclose = function (ev) {
            var closedProjectId = projectId;
            teamWs = null;
            if (ev.code === 4003 || ev.code === 4004) {
                if (activeChannel === 'team' && currentMode === 'chat')
                    setStatus(ev.code === 4004 ? 'Project chat unavailable' : 'Team chat unavailable for this project');
                return;
            }
            if (!window.currentProjectId) {
                setStatus('Select a project to use team chat');
                return;
            }
            if (window.currentProjectId !== closedProjectId) return;
            if (activeChannel === 'team' && currentMode === 'chat') setStatus('Reconnecting...');
            setTimeout(function () {
                if (window.currentProjectId === closedProjectId) connectTeam(window.currentProjectId);
            }, 3000);
        };
        teamWs.onerror = function () { /* handled by onclose */ };
    }

    function handleIncoming(ev, channel) {
        var data;
        try { data = JSON.parse(ev.data); } catch (e) { return; }
        if (data.type !== 'message') return;
        messageStore[channel].push(data);

        if (currentMode === 'chat' && isChatOpen()) {
            var container = document.getElementById('chatMessages');
            if (container) {
                var isSelf = data.sender_username === currentUsername;
                appendMsgEl(data, isSelf, container);
            }
        } else {
            // increment unread
            unreadTeam++;
            updateBadge();
        }
    }

    // ─── History loading ──────────────────────────────────────────────────────
    function loadHistory(channel, projectId) {
        var pid = projectId || window.currentProjectId;
        if (!pid) {
            messageStore[channel] = [];
            if (currentMode === 'chat' && activeChannel === channel) renderMessages();
            return;
        }
        fetch('/api/chat/messages?limit=80&project_id=' + pid)
            .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
            .then(function (d) {
                messageStore[channel] = d.messages || [];
                if (currentMode === 'chat' && activeChannel === channel) renderMessages();
            })
            .catch(function () {
                messageStore[channel] = [];
                if (currentMode === 'chat' && activeChannel === channel) renderMessages();
            });
    }

    // ─── Channel / Mode switching ─────────────────────────────────────────────
    function switchChannel(channel) {
        activeChannel = 'team'; // Only team remains
        unreadTeam = 0;
        updateBadge();

        var title = document.getElementById('chatPanelTitle');
        if (title) title.textContent = 'Team Chat';

        if (!hasActiveProject()) {
            setStatus('Select a project to use team chat');
        } else {
            if (!teamWs || teamWs.readyState > WebSocket.OPEN) connectTeam(window.currentProjectId);
            setStatus(teamChatStatus());
        }

        renderMessages();
    }

    function setMode(mode) {
        currentMode = mode;
        var chatBtn = document.getElementById('chatModeChat');
        var aiBtn = document.getElementById('chatModeAi');
        if (chatBtn) chatBtn.classList.toggle('chat-mode-active', mode === 'chat');
        if (aiBtn) aiBtn.classList.toggle('chat-mode-active', mode === 'ai');

        var title = document.getElementById('chatPanelTitle');
        if (title) {
            if (mode === 'ai') title.textContent = 'AI Assistant';
            else title.textContent = 'Team Chat';
        }

        var tabRow = document.getElementById('chatChannelTabRow');
        if (tabRow) tabRow.style.display = 'none';

        var input = document.getElementById('chatInput');
        if (input) input.placeholder = mode === 'chat' ? 'Type a message…' : 'Ask me anything…';

        var voiceWrap = document.getElementById('chatVoiceWrap');
        if (voiceWrap) voiceWrap.style.display = mode === 'ai' ? '' : 'none';

        if (mode === 'chat' && mediaRecorder && mediaRecorder.state === 'recording') stopRecording();

        if (mode === 'ai') {
            setStatus('AI Assistant · GPT-4o');
        } else {
            if (!hasActiveProject()) {
                setStatus('Select a project to use team chat');
            } else {
                if (!teamWs || teamWs.readyState > WebSocket.OPEN) connectTeam(window.currentProjectId);
                setStatus(teamChatStatus());
            }
        }
        renderMessages();
    }

    // ─── Open / Close ─────────────────────────────────────────────────────────
    function openChat() {
        var wrap = document.getElementById('chatWidgetWrap');
        if (wrap) wrap.classList.add('chat-open');
        unreadTeam = 0;
        updateBadge();
        if (currentMode === 'chat') {
            if (hasActiveProject()) {
                if (!teamWs || teamWs.readyState > WebSocket.OPEN) connectTeam(window.currentProjectId);
                loadHistory(activeChannel);
                setStatus(teamChatStatus());
            } else {
                setStatus('Select a project to use team chat');
            }
        } else {
            setStatus('AI Assistant · GPT-4o');
        }
        renderMessages();
    }

    function closeChat() {
        var wrap = document.getElementById('chatWidgetWrap');
        if (wrap) wrap.classList.remove('chat-open');
    }

    // ─── Send ─────────────────────────────────────────────────────────────────
    function sendMessage() {
        var input = document.getElementById('chatInput');
        if (!input) return;
        var text = (input.value || '').trim();
        if (!text) return;

        if (currentMode === 'chat') {
            if (!hasActiveProject()) {
                setStatus('Select a project to use team chat');
                return;
            }
            if (!teamWs || teamWs.readyState !== WebSocket.OPEN) {
                setStatus('Not connected - cannot send');
                connectTeam(window.currentProjectId);
                return;
            }
            teamWs.send(JSON.stringify({ type: 'message', text: text }));
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
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var el = document.getElementById(typingId);
                if (el) el.remove();
                var reply = (data.reply || 'No response.').trim();
                aiMessages.push({ sender_username: 'AI', sender_role: 'assistant', message: reply, created_at: new Date().toISOString() });
                renderMessages();
            })
            .catch(function () {
                var el = document.getElementById(typingId);
                if (el) el.remove();
                aiMessages.push({ sender_username: 'AI', sender_role: 'assistant', message: 'Sorry, something went wrong.', created_at: new Date().toISOString() });
                renderMessages();
            });
    }

    // ─── Voice recording ──────────────────────────────────────────────────────
    function stopRecording() {
        if (mediaRecorder && mediaRecorder.state === 'recording') mediaRecorder.stop();
        if (recordingStream) { recordingStream.getTracks().forEach(function (t) { t.stop(); }); recordingStream = null; }
        mediaRecorder = null;
        var btn = document.getElementById('chatVoiceBtn');
        if (btn) { btn.classList.remove('chat-voice-recording'); btn.title = 'Record voice message'; }
    }

    function startRecording() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { window.appNotice('Microphone not supported in this browser.', { type: 'error' }); return; }
        recordingChunks = [];
        navigator.mediaDevices.getUserMedia({ audio: true })
            .then(function (stream) {
                recordingStream = stream;
                var mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : 'audio/webm';
                try { mediaRecorder = new MediaRecorder(stream); } catch (e) { stream.getTracks().forEach(function (t) { t.stop(); }); window.appNotice('Recording not supported: ' + e.message, { type: 'error' }); return; }
                mediaRecorder.ondataavailable = function (e) { if (e.data.size) recordingChunks.push(e.data); };
                mediaRecorder.onstop = function () {
                    stream.getTracks().forEach(function (t) { t.stop(); }); recordingStream = null;
                    var btn = document.getElementById('chatVoiceBtn');
                    if (btn) { btn.classList.remove('chat-voice-recording'); btn.title = 'Record voice message'; }
                    if (!recordingChunks.length) return;
                    sendVoiceToAi(new Blob(recordingChunks, { type: mime }));
                };
                mediaRecorder.start(200);
                var btn = document.getElementById('chatVoiceBtn');
                if (btn) { btn.classList.add('chat-voice-recording'); btn.title = 'Stop recording'; }
            })
            .catch(function (err) { window.appNotice('Microphone access denied: ' + (err.message || 'Unknown error'), { type: 'error' }); });
    }

    function toggleVoiceRecording() {
        if (currentMode !== 'ai') return;
        if (mediaRecorder && mediaRecorder.state === 'recording') { stopRecording(); return; }
        startRecording();
    }

    function sendVoiceToAi(audioBlob) {
        var now = new Date().toISOString();
        var typingId = 'ai-typing-' + Date.now();
        var container = document.getElementById('chatMessages');
        var typingDiv = document.createElement('div');
        typingDiv.id = typingId; typingDiv.className = 'chat-msg msg-other chat-msg-typing';
        typingDiv.innerHTML = '<div class="chat-msg-sender">AI</div><div class="chat-msg-text">Listening & thinking…</div>';
        container.appendChild(typingDiv); container.scrollTop = container.scrollHeight;
        var fd = new FormData();
        fd.append('audio', audioBlob, 'voice.webm');
        fetch('/api/chat/ai/voice', { method: 'POST', body: fd })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var el = document.getElementById(typingId); if (el) el.remove();
                var transcript = (data.transcript || '').trim();
                var reply = (data.reply || '').trim();
                if (transcript) aiMessages.push({ sender_username: currentUsername, sender_role: 'user', message: transcript, created_at: now });
                if (reply) aiMessages.push({ sender_username: 'AI', sender_role: 'assistant', message: reply, created_at: new Date().toISOString() });
                renderMessages();
            })
            .catch(function () {
                var el = document.getElementById(typingId); if (el) el.remove();
                aiMessages.push({ sender_username: 'AI', sender_role: 'assistant', message: 'Voice request failed.', created_at: new Date().toISOString() });
                renderMessages();
            });
    }

    // ─── Init ─────────────────────────────────────────────────────────────────
    function buildChannelTabs() {
        // No longer rendering Global / Team tabs
    }

    function initChatWidget() {
        currentUsername = getCookie('username');
        currentRole = getCookie('role');
        if (!currentUsername) return;

        buildChannelTabs();

        var toggleBtn = document.getElementById('chatToggleBtn');
        var closeBtn = document.getElementById('chatPanelClose');
        var sendBtn = document.getElementById('chatSendBtn');
        var input = document.getElementById('chatInput');
        var modeChat = document.getElementById('chatModeChat');
        var modeAi = document.getElementById('chatModeAi');
        var voiceBtn = document.getElementById('chatVoiceBtn');

        if (toggleBtn) toggleBtn.addEventListener('click', function () {
            var wrap = document.getElementById('chatWidgetWrap');
            if (wrap && wrap.classList.contains('chat-open')) closeChat(); else openChat();
        });
        if (closeBtn) closeBtn.addEventListener('click', closeChat);
        if (sendBtn) sendBtn.addEventListener('click', sendMessage);
        if (input) {
            input.addEventListener('keydown', function (e) {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
            });
        }
        if (modeChat) modeChat.addEventListener('click', function () { setMode('chat'); });
        if (modeAi) modeAi.addEventListener('click', function () { setMode('ai'); });
        if (voiceBtn) voiceBtn.addEventListener('click', toggleVoiceRecording);

        // Connect team channel (if project is active)
        if (window.currentProjectId) connectTeam(window.currentProjectId);
        else setStatus('Select a project to use team chat');

        setMode('chat');
        var voiceWrap = document.getElementById('chatVoiceWrap');
        if (voiceWrap) voiceWrap.style.display = 'none';
    }

    // ─── Public API ───────────────────────────────────────────────────────────
    // Called by developer dashboard when switching the active project in the sidebar.
    window.switchTeamProject = function (projectId, projectTitle) {
        if (!projectId) return;
        // Close old team connection
        if (teamWs) {
            teamWs.onclose = null; // prevent reconnect loop
            teamWs.close();
            teamWs = null;
        }
        // Reset team message store
        messageStore.team = [];
        unreadTeam = 0;
        updateBadge();
        // Switch channel completely connects to team project connection
        window.currentProjectId = projectId;
        connectTeam(projectId);

        // If team channel is active, reload history + show
        if (activeChannel === 'team') {
            renderMessages();
            setStatus('Connecting...');
        }
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initChatWidget);
    } else {
        initChatWidget();
    }
})();
