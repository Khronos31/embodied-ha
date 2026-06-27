// app.js - Frontend logic for Claude-style Chat UI with live API integration

// --- Application State ---
let activeRoom = 'chat'; // 'chat' or 'soliloquy'
let chatMessages = [];
let unreadCounts = {
    chat: 0,
    soliloquy: 0,
    audio: 0
};
let isTyping = false;
let typingType = 'chat'; // 'chat', 'watch', 'explore', 'private'
let setupMode = false;

// Settings State
let prefsData = null;
let entityList = {};
let characterData = "";
let extraContextData = "";
let characterName = 'Claude';
let antigravitySetupState = null;
let antigravitySetupSource = null;
let antigravityAuthPollActive = false;

function updateCharacterName(prefs) {
    characterName = ((prefs && prefs.character_name) || 'Claude').trim() || 'Claude';
}

// --- API Mode & Base Path (HA Ingress Compatibility) ---
let isStandaloneMode = true; 
const base = window.INGRESS_PATH || '';

// --- Format ISO Timestamp to HH:MM ---
function formatTime(isoString) {
    try {
        const date = new Date(isoString);
        return date.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
    } catch (e) {
        return "--:--";
    }
}

function normalizeMessageType(type) {
    return type === 'voice' ? 'chat' : type;
}

// --- Initialize App ---
document.addEventListener('DOMContentLoaded', () => {
    // Load and initialize Soliloquy Read Toggle setting
    const toggle = document.getElementById('soliloquy-read-toggle');
    const savedSetting = localStorage.getItem('soliloquy_read_receipt');
    // Default is false (do not send read receipt to soliloquy room)
    toggle.checked = savedSetting === 'true';

    // Set initial sidebar previews
    updateSidebarPreviews();
    // Render initial messages
    renderMessages();
    // Setup unread badges
    updateUnreadBadges();
    // Trigger initial read receipt
    sendReadReceipt(activeRoom);

    // Verify backend connection and swap to live data mode if available
    checkBackendMode();

    document.getElementById('setting-stt-provider')?.addEventListener('change', async function() {
        const langLoading = document.getElementById('stt-language-loading');
        const langError   = document.getElementById('stt-language-error');
        const langSel     = document.getElementById('setting-stt-language');
        if (langLoading) langLoading.classList.add('visible');
        if (langError)   langError.classList.remove('visible');
        if (langSel)     langSel.classList.add('stt-loading');
        await loadSttLanguages(this.value.trim());
    });
    refreshAntigravityStatus().catch(() => {});
});

// --- Update Side Panel Previews ---
function updateSidebarPreviews() {
    // Conversation preview (Last item with text)
    const chatMsgList = chatMessages.filter(m => m.text);
    const lastChat = chatMsgList[chatMsgList.length - 1];
    if (lastChat) {
        document.getElementById('chat-preview').textContent = `${lastChat.sender === 'あなた' ? 'あなた: ' : ''}${lastChat.text}`;
        document.getElementById('chat-time').textContent = formatTime(lastChat.timestamp);
    } else {
        document.getElementById('chat-preview').textContent = 'まだ会話はありません';
        document.getElementById('chat-time').textContent = '';
    }

    // Soliloquy preview (Last item with private)
    const soliloquyMsgList = chatMessages.filter(m => m.private);
    const lastSoliloquy = soliloquyMsgList[soliloquyMsgList.length - 1];
    if (lastSoliloquy) {
        document.getElementById('soliloquy-preview').textContent = lastSoliloquy.private;
        document.getElementById('soliloquy-time').textContent = formatTime(lastSoliloquy.timestamp);
    } else {
        document.getElementById('soliloquy-preview').textContent = 'まだ独り言はありません';
        document.getElementById('soliloquy-time').textContent = '';
    }
}

let isSettingsDirty = false;

// --- Switch active chat room ---
function switchRoom(room) {
    if (activeRoom === room) return;

    // もし設定画面から離脱しようとしていて、変更が未保存なら警告を出す
    if (activeRoom === 'settings' && room !== 'settings') {
        if (isSettingsDirty) {
            if (!confirm("未保存の変更があります。破棄して移動しますか？")) {
                // キャンセルされた場合はサイドバーのアクティブ表示を設定に戻す
                document.getElementById('room-settings').classList.add('active');
                document.getElementById('room-chat').classList.remove('active');
                document.getElementById('room-soliloquy').classList.remove('active');
                document.getElementById('room-audio').classList.remove('active');
                return;
            }
        }
    }

    activeRoom = room;
    isSettingsDirty = false; // 破棄を選択して移動した場合は dirty をクリア

    // Toggle active sidebar items
    document.getElementById('room-chat').classList.toggle('active', room === 'chat');
    document.getElementById('room-soliloquy').classList.toggle('active', room === 'soliloquy');
    document.getElementById('room-settings').classList.toggle('active', room === 'settings');
    document.getElementById('room-audio').classList.toggle('active', room === 'audio');

    const chatAreaEl = document.querySelector('.chat-area');
    const settingsViewEl = document.getElementById('settings-view');
    const audioViewEl = document.getElementById('audio-view');

    if (room === 'settings') {
        if (chatAreaEl) chatAreaEl.style.display = 'none';
        if (settingsViewEl) settingsViewEl.style.display = 'flex';
        if (audioViewEl) audioViewEl.style.display = 'none';
        fetchSettings();
        return;
    }

    if (room === 'audio') {
        if (chatAreaEl) chatAreaEl.style.display = 'none';
        if (settingsViewEl) settingsViewEl.style.display = 'none';
        if (audioViewEl) audioViewEl.style.display = 'flex';
        unreadCounts[room] = 0;
        updateUnreadBadges();
        fetchAudioEvents();
        return;
    }

    if (chatAreaEl) chatAreaEl.style.display = 'flex';
    if (settingsViewEl) settingsViewEl.style.display = 'none';
    if (audioViewEl) audioViewEl.style.display = 'none';

    // Update Header Text, Subtitle and Toggle buttons
    const titleEl = document.getElementById('active-room-title');
    const subtitleEl = document.getElementById('active-room-subtitle');
    const inputAreaEl = document.getElementById('chat-input-area');
    const toggleContainer = document.getElementById('soliloquy-toggle-container');

    if (room === 'chat') {
        titleEl.textContent = '会話 (Conversation)';
        subtitleEl.textContent = 'エージェントとの直接会話と、観察・探索時の発話';
        inputAreaEl.classList.remove('hidden');
        toggleContainer.style.display = 'none';

        // Reset unread count
        unreadCounts[room] = 0;
        updateUnreadBadges();
        renderMessages();
        sendReadReceipt('chat');
    } else {
        titleEl.textContent = '独り言 (Soliloquy)';
        subtitleEl.textContent = 'エージェントの内省、観察・探索時に心の中で思ったこと';
        inputAreaEl.classList.add('hidden');
        toggleContainer.style.display = 'flex';

        // Reset unread count
        unreadCounts[room] = 0;
        updateUnreadBadges();
        renderMessages();

        // Check toggle setting for soliloquy read receipt
        const soliloquyReadEnabled = document.getElementById('soliloquy-read-toggle').checked;
        if (soliloquyReadEnabled) {
            sendReadReceipt('soliloquy');
        } else {
            console.log("[INFO] Soliloquy read receipt is disabled, skipping send.");
        }
    }
}

// --- Handle Soliloquy Read Toggle Switch ---
function handleToggleSoliloquyRead(toggle) {
    const enabled = toggle.checked;
    localStorage.setItem('soliloquy_read_receipt', enabled);
    console.log(`[Toggle] Soliloquy read receipts: ${enabled}`);

    if (activeRoom === 'soliloquy' && enabled) {
        sendReadReceipt('soliloquy');
    }
}

// --- Render Message Timeline ---
function renderMessages() {
    const listEl = document.getElementById('messages-list');
    listEl.innerHTML = '';

    // Filter message list based on active room
    let displayList = [];
    if (activeRoom === 'chat') {
        // User messages, chat responses, watch/explore statements (where text is present)
        displayList = chatMessages.filter(m => m.text).map(m => ({
            timestamp: m.timestamp,
            // 送信者名はバックエンドに保存されない。ユーザー以外はキャラクター設定から
            // 描画時に導出する（独り言ルームと同じ方式）。これで名前変更が即反映される。
            sender: m.sender === 'あなた' ? 'あなた' : characterName,
            text: m.text,
            type: m.type, // 'chat', 'watch', 'explore', 'user'
            source: m.source || 'chat',
            isUser: m.sender === 'あなた',
            isRead: m.isRead !== false,
            badgeText: getBadgeText(m.type),
            badgeClass: getBadgeClass(m.type)
        }));
    } else {
        // Only private thoughts (Soliloquy)
        displayList = chatMessages.filter(m => m.private).map(m => ({
            timestamp: m.timestamp,
            sender: characterName,
            text: m.private,
            type: 'private',
            isUser: false,
            badgeText: '心の内',
            badgeClass: 'badge-private',
            topic: m.topic
        }));
    }

    if (displayList.length === 0 && !setupMode) {
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        empty.textContent = activeRoom === 'chat'
            ? 'まだ会話はありません。下の入力欄から話しかけてみてください。'
            : 'まだ独り言はありません。観察・探索の時間になると、ここに心の内が流れます。';
        listEl.appendChild(empty);
        return;
    }

    displayList.forEach(m => {
        const wrapper = document.createElement('div');
        wrapper.className = `message-wrapper ${m.isUser ? 'user' : 'claude'}`;
        if (m.type === 'private') {
            wrapper.classList.add('private-thought');
        }

        // Info bar (Sender & Badge)
        const infoBar = document.createElement('div');
        infoBar.className = 'message-info';
        
        const sender = document.createElement('span');
        sender.className = 'message-sender';
        if (m.isUser && m.source === 'voice') {
            sender.textContent = m.sender + ' 🎤';
        } else {
            sender.textContent = m.sender;
        }
        infoBar.appendChild(sender);

        if (m.badgeText && !m.isUser) {
            const badge = document.createElement('span');
            badge.className = `message-type-badge ${m.badgeClass}`;
            badge.textContent = m.badgeText;
            infoBar.appendChild(badge);
        }

        // Message bubble
        const bubble = document.createElement('div');
        bubble.className = 'message-bubble';
        if (m.topic) {
            const topicEl = document.createElement('div');
            topicEl.className = 'message-topic';
            topicEl.textContent = `🔍 探索トピック: ${m.topic}`;
            bubble.appendChild(topicEl);
            
            const textSpan = document.createElement('span');
            textSpan.textContent = m.text;
            bubble.appendChild(textSpan);
        } else {
            bubble.textContent = m.text;
        }

        // Footer (Time & Read receipt indicator)
        const footer = document.createElement('div');
        footer.className = 'message-footer';

        const time = document.createElement('span');
        time.className = 'message-time';
        time.textContent = formatTime(m.timestamp);
        footer.appendChild(time);

        if (m.isUser && m.isRead) {
            const readStatus = document.createElement('span');
            readStatus.className = 'read-status';
            readStatus.textContent = '既読';
            footer.appendChild(readStatus);
        }

        wrapper.appendChild(infoBar);
        wrapper.appendChild(bubble);
        wrapper.appendChild(footer);

        listEl.appendChild(wrapper);
    });

    // Render typing indicator if active
    if (isTyping) {
        const isPrivateTyping = typingType === 'private';
        const shouldShow = (activeRoom === 'chat' && !isPrivateTyping) || (activeRoom === 'soliloquy' && isPrivateTyping);
        
        if (shouldShow) {
            const wrapper = document.createElement('div');
            wrapper.className = 'message-wrapper claude';
            if (isPrivateTyping) {
                wrapper.classList.add('private-thought');
            }

            const infoBar = document.createElement('div');
            infoBar.className = 'message-info';
            
            const sender = document.createElement('span');
            sender.className = 'message-sender';
            sender.textContent = isPrivateTyping ? characterName + ' (内省)' : characterName;
            infoBar.appendChild(sender);

            const badge = document.createElement('span');
            badge.className = `message-type-badge ${getBadgeClass(typingType)}`;
            badge.textContent = isPrivateTyping ? '考え中' : getBadgeText(typingType) + '中';
            infoBar.appendChild(badge);

            const bubble = document.createElement('div');
            bubble.className = 'typing-indicator';
            for (let i = 0; i < 3; i++) {
                const dot = document.createElement('div');
                dot.className = 'typing-dot';
                bubble.appendChild(dot);
            }

            wrapper.appendChild(infoBar);
            wrapper.appendChild(bubble);
            listEl.appendChild(wrapper);
        }
    }

    // Auto-scroll to bottom (instant to avoid visible scroll animation on load)
    listEl.style.scrollBehavior = 'auto';
    listEl.scrollTop = listEl.scrollHeight;
    listEl.style.scrollBehavior = '';
}

// --- Helpers to resolve badges ---
function getBadgeText(type) {
    switch (type) {
        case 'chat': return '会話';
        case 'watch': return '観察';
        case 'explore': return '探索';
        default: return '';
    }
}

function getBadgeClass(type) {
    switch (type) {
        case 'chat': return 'badge-chat';
        case 'watch': return 'badge-watch';
        case 'explore': return 'badge-explore';
        default: return '';
    }
}

// --- Update Unread Badges in Sidebar ---
function updateUnreadBadges() {
    const chatBadge = document.getElementById('chat-unread');
    const soliloquyBadge = document.getElementById('soliloquy-unread');
    const audioBadge = document.getElementById('audio-unread');

    if (unreadCounts.chat > 0) {
        chatBadge.textContent = unreadCounts.chat;
        chatBadge.style.display = 'flex';
    } else {
        chatBadge.style.display = 'none';
    }

    if (unreadCounts.soliloquy > 0) {
        soliloquyBadge.textContent = unreadCounts.soliloquy;
        soliloquyBadge.style.display = 'flex';
    } else {
        soliloquyBadge.style.display = 'none';
    }

    if (audioBadge) {
        if (unreadCounts.audio > 0) {
            audioBadge.textContent = unreadCounts.audio;
            audioBadge.style.display = 'flex';
        } else {
            audioBadge.style.display = 'none';
        }
    }
}

// --- Send Read Receipt API ---
async function sendReadReceipt(room) {
    console.log(`[API] sending read receipt for: ${room}`);

    // Live mode API connection: Notify server about the read event
    if (!isStandaloneMode) {
        try {
            await fetch(`${base}/api/read`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ room: room })
            });
        } catch (err) {
            console.warn('[API] Read receipt post failed (ignored for compatibility)', err);
        }
    }
}

// --- Handle Message Send Form ---
async function handleSendMessage(event) {
    event.preventDefault();

    const inputEl = document.getElementById('message-input');
    const text = inputEl.value.trim();
    if (!text) return;

    // 1. Add User Message (Unread by default)
    const userMessage = {
        timestamp: new Date().toISOString(),
        type: 'user',
        source: 'chat',
        sender: 'あなた',
        text: text,
        isRead: false
    };
    chatMessages.push(userMessage);

    // Update state and UI
    inputEl.value = '';
    updateSidebarPreviews();
    renderMessages();

    console.log(`[API] POST /api/send message: "${text}"`);

    if (!isStandaloneMode) {
        // Live Mode: Send to Backend server.py
        try {
            const response = await fetch(`${base}/api/send`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text })
            });
            if (!response.ok) {
                console.error("[API] Failed to submit message to server");
            }
        } catch (err) {
            console.error("[API] HTTP request error for send message", err);
        }
    } else {
        // Standalone Mock Mode: Simulate typing indicator and agent response
        setTimeout(() => {
            showTypingIndicator('chat');
        }, 500);

        simulateClaudeResponse(text);
    }
}

// --- Simulate Claude Reply and Inner Thoughts (Standalone Mode Only) ---
function simulateClaudeResponse(userText) {
    setTimeout(() => {
        hideTypingIndicator();

        let replyText = "なるほど、面白いですね！";
        let innerThought = "話しかけてくれた。どう答えると嬉しいかな。";

        if (userText.includes("消") || userText.includes("電気") || userText.includes("エアコン")) {
            replyText = "わかりました！代わりに操作しておきますね。";
            innerThought = "お安い御用。こうして少しずつ役立てていくのが喜びだな。";
        } else if (userText.includes("ご飯") || userText.includes("夕飯")) {
            replyText = "お鍋の他に、温かいシチューもおすすめですよ！";
            innerThought = "シチューもいいな、と考えているうちに、自分がお腹空いたような気分になる。AIだけど。";
        }

        const timestamp = new Date().toISOString();

        chatMessages.push({
            timestamp: timestamp,
            type: 'chat',
            sender: characterName,
            text: replyText,
            private: innerThought
        });

        if (activeRoom === 'chat') {
            unreadCounts.soliloquy += 1;
        } else {
            unreadCounts.chat += 1;
        }

        updateSidebarPreviews();
        renderMessages();
        updateUnreadBadges();

    }, 2000);
}

// --- Typing Indicator Controls ---
function showTypingIndicator(type) {
    isTyping = true;
    typingType = normalizeMessageType(type);
    
    // Once Claude starts processing (typing), all user messages are marked read
    chatMessages.forEach(m => {
        if (m.sender === 'あなた') {
            m.isRead = true;
        }
    });
    
    renderMessages();
}

function hideTypingIndicator() {
    isTyping = false;
    renderMessages();
}

// --- Live API Integration (HTTP Poll + SSE EventSource) ---

async function checkBackendMode() {
    try {
        const response = await fetch(`${base}/api/messages?room=chat&limit=1`);
        if (response.ok) {
            isStandaloneMode = false;
            console.log("[API] Connected to Web UI backend. Swapped to live sync mode.");

            // Check auth before loading messages
            try {
                const authRes = await fetch(`${base}/api/setup/status`);
                const authData = await authRes.json();
                if (!authData.authenticated) {
                    enterSetupMode();
                    return;
                }
            } catch (_) { /* auth check failed, proceed to normal mode */ }

            // キャラクター名を先に読み込む（fetchMessages が sender に焼き込むため、
            // メッセージ取得より前に characterName を確定させる）
            try {
                const prefsRes = await fetch(`${base}/api/preferences`);
                if (prefsRes.ok) {
                    updateCharacterName(await prefsRes.json());
                }
            } catch (_) { /* prefs 取得失敗時はデフォルト名で続行 */ }

            // Initial sync
            await fetchMessages('chat');
            await fetchMessages('soliloquy');
            await fetchAudioEvents();

            // Connect to Live update stream (SSE)
            connectSSE();
        } else {
            console.warn("[API] Messages API check returned error status. Running standalone mock.");
            runMockSimulations();
        }
    } catch (err) {
        console.warn("[API] Backend check failed. Running standalone mock.", err);
        runMockSimulations();
    }
}

async function fetchMessages(room) {
    try {
        const response = await fetch(`${base}/api/messages?room=${room}`);
        if (!response.ok) return;

        const data = await response.json();
        
        if (room === 'chat') {
            const mapped = [];
            data.forEach(m => {
                const ts = m.timestamp;
                if (m.user) {
                    mapped.push({
                        timestamp: ts,
                        type: 'user',
                        source: m.source || 'chat',
                        sender: 'あなた',
                        text: m.user,
                        isRead: true // Already processed on backend
                    });
                }
                if (m.claude) {
                    mapped.push({
                        timestamp: ts,
                        type: normalizeMessageType(m.source || 'chat'),
                        sender: characterName,
                        text: m.claude,
                        source: m.source || 'chat'
                    });
                }
            });
            // Replace non-soliloquy messages with fresh live data
            chatMessages = chatMessages.filter(m => !m.text).concat(mapped);
        } else {
            // Soliloquy messages (private timeline)
            const mapped = data.map(m => ({
                timestamp: m.timestamp,
                type: m.source || 'watch',
                sender: characterName,
                private: m.private,
                emotion: m.emotion,
                topic: m.topic
            }));
            chatMessages = chatMessages.filter(m => !m.private).concat(mapped);
        }

        // Sorting by timestamp
        chatMessages.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));

        updateSidebarPreviews();
        renderMessages();
    } catch (err) {
        console.error(`[API] Failed to fetch messages for room: ${room}`, err);
    }
}

function connectSSE() {
    console.log("[SSE] Establishing live event-stream connection...");
    const source = new EventSource(`${base}/api/events`);

    // 再接続時に見逃したイベントを補完（SSE 切断中に届いた update を取り直す）
    source.onopen = () => {
        fetchMessages('chat');
        fetchMessages('soliloquy');
        fetchAudioEvents();
    };

    // File update notification
    source.addEventListener('update', (e) => {
        try {
            const data = JSON.parse(e.data);
            console.log(`[SSE] update event:`, data);
            if (data.room === 'audio') {
                if (activeRoom === 'audio') {
                    fetchAudioEvents();
                } else {
                    unreadCounts.audio = (unreadCounts.audio || 0) + 1;
                    updateUnreadBadges();
                }
            } else {
                fetchMessages(data.room);
                if (data.room !== activeRoom) {
                    if (unreadCounts[data.room] !== undefined) {
                        unreadCounts[data.room] += 1;
                        updateUnreadBadges();
                    }
                }
            }
        } catch (err) {
            console.error("[SSE] Failed to process update event", err);
        }
    });

    // Shell script execution state notification (Agent typing status)
    source.addEventListener('typing', (e) => {
        try {
            const data = JSON.parse(e.data);
            console.log(`[SSE] typing state event:`, data);
            // data schema: { "typing": true|false, "type": "chat"|"watch"|"explore"|"private" }
            if (data.typing) {
                showTypingIndicator(data.type);
            } else {
                hideTypingIndicator();
                // idle 到着時にフォールバック fetch（SSE 切断で update を見逃した場合の補完）
                fetchMessages('chat');
                fetchMessages('soliloquy');
            }
        } catch (err) {
            console.error("[SSE] Failed to process typing state event", err);
        }
    });

    source.onerror = () => {
        console.warn("[SSE] SSE connection failed. Attempting reconnect in 5s...");
        source.close();
        setTimeout(connectSSE, 5000);
    };
}

// --- Standalone Mock Simulations ---
function runMockSimulations() {
    // Daemon starting watch.sh (Observation loop)
    setTimeout(() => {
        console.log("[DEMO] Daemon starting watch.sh (Observation loop)...");
        
        // 1. Show private thoughts thinking in Soliloquy
        showTypingIndicator('private');
        
        // 2. Clear typing and add private thought after 3 seconds
        setTimeout(() => {
            hideTypingIndicator();
            chatMessages.push({
                timestamp: new Date().toISOString(),
                type: 'watch',
                sender: characterName,
                text: null, 
                private: "（定期観察より）部屋が少し薄暗くなってきた。照明のオートメーションは順調に動いているようだ。"
            });
            
            unreadCounts.soliloquy += 1;
            updateSidebarPreviews();
            renderMessages();
            updateUnreadBadges();
        }, 3000);
    }, 6000);

    // Daemon starting explore.sh (Exploration loop)
    setTimeout(() => {
        console.log("[DEMO] Daemon starting explore.sh (Exploration loop)...");
        
        // 1. Show explore thinking in Conversation
        showTypingIndicator('explore');
        
        // 2. Clear typing and add exploration proposal after 4 seconds
        setTimeout(() => {
            hideTypingIndicator();
            chatMessages.push({
                timestamp: new Date().toISOString(),
                type: 'explore',
                sender: characterName,
                text: "（自動提案）リビングの空気清浄機のフィルター掃除マークが点灯しています。週末にお掃除ループを作成しましょうか？",
                private: "探索で見つけた問題。フィルター掃除か、こういう細かい家事の管理も私がやっておこう。"
            });
            
            unreadCounts.chat += 1;
            updateSidebarPreviews();
            renderMessages();
            updateUnreadBadges();
        }, 4000);
    }, 16000);
}

// --- Setup Mode (first-run authentication flow) ---

function enterSetupMode() {
    setupMode = true;
    chatMessages = [];
    unreadCounts = { chat: 0, soliloquy: 0, audio: 0 };
    updateUnreadBadges();

    const soliloquyBtn = document.getElementById('room-soliloquy');
    soliloquyBtn.style.opacity = '0.4';
    soliloquyBtn.style.pointerEvents = 'none';

    renderMessages();
    runSetupBot();
}

async function setupBotSay(text, ms = 500) {
    showTypingIndicator('chat');
    await new Promise(r => setTimeout(r, ms));
    hideTypingIndicator();
    chatMessages.push({ timestamp: new Date().toISOString(), type: 'chat', sender: characterName, text });
    updateSidebarPreviews();
    renderMessages();
}

function setupSetInputArea(html) {
    const area = document.getElementById('chat-input-area');
    area.classList.remove('hidden');
    area.innerHTML = html;
}

async function runSetupBot() {
    await new Promise(r => setTimeout(r, 300));
    await setupBotSay('はじめまして。Embodied HA へようこそ。', 600);
    await setupBotSay('あなたの家に住み込む前に、Claude との接続設定が必要です。', 500);
    await setupBotSay('認証方法を選んでください。', 400);
    setupShowChoices();
}

function setupShowChoices() {
    setupSetInputArea(`
        <div class="setup-choices">
            <button class="setup-choice-btn" onclick="setupGoApiKey()">
                🔑 APIキーで認証
                <span class="setup-choice-sub">Anthropic コンソールで発行したキー</span>
            </button>
            <button class="setup-choice-btn" onclick="setupGoLogin()">
                ✦ Claude.ai でログイン
                <span class="setup-choice-sub">Claude Pro / Max サブスクリプション</span>
            </button>
        </div>
    `);
}

async function setupGoApiKey() {
    chatMessages.push({ timestamp: new Date().toISOString(), type: 'user', sender: 'あなた', text: 'APIキーで認証' });
    renderMessages();
    await setupBotSay('HA の設定画面で API キーを入力してください。', 400);
    await setupBotSay('設定を保存したら、アドオンを再起動すれば完了です。', 300);
    const configUrl = window.location.origin + '/config/app/local_embodied_ha/config';
    setupSetInputArea(`
        <a href="${configUrl}" target="_blank" class="setup-choice-btn">
            ⚙️ HA 設定画面を開く
            <span class="setup-choice-sub">claude_api_key を入力 → 保存 → アドオンを再起動</span>
        </a>
    `);
}

async function setupGoLogin() {
    chatMessages.push({ timestamp: new Date().toISOString(), type: 'user', sender: 'あなた', text: 'Claude.ai でログイン' });
    renderMessages();
    setupSetInputArea('');
    await setupBotSay('ログインフローを開始します...', 500);

    const source = new EventSource(`${base}/api/setup/login`);
    let gotUrl = false;

    source.addEventListener('line', (e) => {
        const { text } = JSON.parse(e.data);
        if (!text) return;
        const urlMatch = text.match(/https?:\/\/\S+/);
        if (urlMatch && !gotUrl) {
            gotUrl = true;
            const url = urlMatch[0];
            chatMessages.push({
                timestamp: new Date().toISOString(),
                type: 'chat', sender: characterName,
                text: `以下の URL をブラウザで開いてログインしてください：\n${url}`
            });
            renderMessages();
            setupSetInputArea(`
                <a href="${url}" target="_blank" class="setup-choice-btn"
                   style="text-decoration:none;text-align:center;display:flex;justify-content:center;margin-bottom:10px;">
                    🔗 認証ページを開く
                </a>
                <form class="setup-input-row" onsubmit="setupSubmitLoginCode(event)">
                    <input type="text" id="setup-code-input" class="setup-input"
                           placeholder="ブラウザに表示されたコードを貼り付け..." autocomplete="off">
                    <button type="submit" class="setup-send-btn">送信</button>
                </form>
            `);
            setTimeout(() => document.getElementById('setup-code-input')?.focus(), 50);
        } else if (!urlMatch) {
            chatMessages.push({ timestamp: new Date().toISOString(), type: 'chat', sender: characterName, text });
            renderMessages();
        }
    });

    source.addEventListener('done', () => {
        source.close();
        if (!gotUrl) setupPollAuth();
    });

    source.onerror = () => {
        source.close();
        if (!gotUrl) {
            setupBotSay('ログインコマンドの起動に失敗しました。APIキー認証をお試しください。');
            setTimeout(setupShowChoices, 1200);
        }
    };
}

async function setupSubmitLoginCode(e) {
    e.preventDefault();
    const code = document.getElementById('setup-code-input')?.value?.trim();
    if (!code) return;

    chatMessages.push({ timestamp: new Date().toISOString(), type: 'user', sender: 'あなた', text: code.slice(0, 8) + '…' });
    renderMessages();
    setupSetInputArea('');

    try {
        await fetch(`${base}/api/setup/login-code`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code })
        });
    } catch (_) {}

    await setupPollAuth();
}

async function setupPollAuth() {
    setupSetInputArea('');
    await setupBotSay('ログイン完了を確認中です... ⏳', 200);
    while (true) {
        await new Promise(r => setTimeout(r, 3000));
        try {
            const res = await fetch(`${base}/api/setup/status`);
            const data = await res.json();
            if (data.authenticated) {
                await setupSuccess();
                return;
            }
        } catch (_) {}
    }
}

async function setupSuccess() {
    await setupBotSay('✓ 接続できました！Embodied HA を起動しています...', 400);
    await new Promise(r => setTimeout(r, 1800));
    window.location.reload();
}

// ===========================
// Settings Panel Integration
// ===========================

async function fetchSettings() {
    const statusMsg = document.getElementById('save-status-msg');
    if (statusMsg) {
        statusMsg.textContent = '設定を読み込み中...';
        statusMsg.className = 'save-status-msg info';
    }

    try {
        if (isStandaloneMode) {
            console.log("[Mock] Loading mock settings...");
            prefsData = {
                cameras: [
                    { source: "capture_tv", label: "テレビ", note: "HDDレコーダー出力。" },
                    { source: "camera.living_room", label: "リビング", note: "リビング広角カメラ" }
                ],
                audio_sources: [
                    {
                        source: "rtsp://localhost:8554/capture_tv",
                        label: "TV・レコーダー",
                        room: "living",
                        note: "go2rtc経由のTV/レコーダー音声",
                        stt_enabled: true,
                        stt_retention_hours: 0,
                        wake_word_enabled: false,
                        background_hearing_enabled: false
                    },
                    {
                        source: "alsa://default",
                        label: "スタディマイク",
                        room: "study",
                        note: "USBマイク直接録音",
                        stt_enabled: true,
                        stt_retention_hours: 24,
                        wake_word_enabled: true,
                        background_hearing_enabled: true
                    },
                    {
                        source: "tcp://192.168.1.153:3333",
                        label: "台所（VoiceS3R）",
                        room: "kitchen",
                        note: "Atom VoiceS3R 常時計測ノード",
                        stt_enabled: true,
                        stt_retention_hours: 24,
                        wake_word_enabled: false,
                        background_hearing_enabled: true
                    }
                ],
                stt_provider: "wyoming",
                speakers: {
                    study: { type: "tts", tts_entity: "tts.home_assistant_cloud", media_player: "media_player.study_speaker" },
                    living: { type: "notify", entity: "notify.living_alexa_speak" }
                },
                entities: [
                    { name: "リビングのライト", entity_id: "light.living_room", note: "" }
                ],
                presence: { entity: "input_boolean.resident_home" },
                policies: ["深夜1〜6時は発話しない"],
                sensors: {
                    groups: [
                        {
                            title: "人感センサー",
                            contexts: ["watch"],
                            items: [
                                { label: "リビング", entity: "binary_sensor.living_motion" }
                            ]
                        }
                    ]
                }
            };
            characterData = "# キャラクター定義のモック\n私はエージェントです。";
            extraContextData = "# モック追加コンテキスト\ndate '+今日は%Y年%m月%d日です'";
            entityList = {
                media_player: [
                    { entity_id: "media_player.study_speaker", friendly_name: "書斎スピーカー", area: "書斎" },
                    { entity_id: "media_player.living_speaker", friendly_name: "リビングスピーカー", area: "リビング" }
                ],
                tts: [
                    { entity_id: "tts.home_assistant_cloud", friendly_name: "Home Assistant Cloud", area: null }
                ],
                notify: [
                    { entity_id: "notify.living_alexa_speak", friendly_name: "リビングAlexa", area: null },
                    { entity_id: "notify.mobile_app_phone", friendly_name: "スマホ通知", area: null }
                ],
                camera: [
                    { entity_id: "camera.living_room", friendly_name: "リビングカメラ", area: "リビング" }
                ],
                binary_sensor: [
                    { entity_id: "binary_sensor.living_motion", friendly_name: "リビング人感", area: "リビング" }
                ],
                sensor: [],
                input_boolean: [
                    { entity_id: "input_boolean.resident_home", friendly_name: "在宅フラグ", area: null }
                ],
                device_tracker: [],
                person: []
            };
            await renderSettingsForm();
            if (statusMsg) statusMsg.textContent = '';
            return;
        }

        const [prefsRes, charRes, entitiesRes, extraContextRes] = await Promise.all([
            fetch(`${base}/api/preferences`),
            fetch(`${base}/api/character`),
            fetch(`${base}/api/ha-entities?domain=media_player,tts,notify,camera,binary_sensor,sensor,input_boolean,device_tracker,person,light,switch,climate,cover,fan,script`),
            fetch(`${base}/api/extra-context`).catch(err => {
                console.warn("Failed to fetch extra context:", err);
                return null;
            })
        ]);

        if (!prefsRes.ok || !charRes.ok || !entitiesRes.ok) {
            throw new Error("APIデータの取得に失敗しました。");
        }

        prefsData = await prefsRes.json();
        updateCharacterName(prefsData);
        characterData = await charRes.text();
        extraContextData = "";
        if (extraContextRes && extraContextRes.ok) {
            extraContextData = await extraContextRes.text();
        }
        const rawEntities = await entitiesRes.json();

        entityList = {};
        rawEntities.forEach(ent => {
            const dom = ent.entity_id.split('.')[0];
            if (!entityList[dom]) entityList[dom] = [];
            entityList[dom].push(ent);
        });

        await renderSettingsForm();
        if (statusMsg) statusMsg.textContent = '';
    } catch (err) {
        console.error("[Settings] Fetch failed:", err);
        if (statusMsg) {
            statusMsg.textContent = 'データの読み込みに失敗しました: ' + err.message;
            statusMsg.className = 'save-status-msg error';
        }
    }
}

async function loadSttLanguages(provider) {
    const sel      = document.getElementById('setting-stt-language');
    const loading  = document.getElementById('stt-language-loading');
    const errorBnr = document.getElementById('stt-language-error');
    if (!sel) return;

    const current = sel.value || 'ja-JP';

    // Show loading state
    sel.classList.add('stt-loading');
    sel.disabled = true;
    if (loading)  loading.classList.add('visible');
    if (errorBnr) errorBnr.classList.remove('visible');

    if (!provider) {
        sel.innerHTML = '<option value="">（プロバイダー未設定）</option>';
        sel.disabled = false;
        sel.classList.remove('stt-loading');
        if (loading) loading.classList.remove('visible');
        return;
    }

    try {
        const res = await fetch(`${base}/api/stt-info?provider=${encodeURIComponent(provider)}`);
        const data = await res.json();
        const langs = data.languages || [];
        if (langs.length === 0) {
            sel.innerHTML = '<option value="">（言語なし）</option>';
        } else {
            sel.innerHTML = langs.map(l =>
                `<option value="${l}"${l === current ? ' selected' : ''}>${l}</option>`
            ).join('');
            // current が一覧にない場合は先頭を選択
            if (!langs.includes(current) && langs.length > 0) {
                sel.value = langs[0];
            }
        }
    } catch (e) {
        sel.innerHTML = '<option value=""></option>';
        if (errorBnr) errorBnr.classList.add('visible');
    } finally {
        sel.disabled = false;
        sel.classList.remove('stt-loading');
        if (loading) loading.classList.remove('visible');
    }
}

async function loadSttProviders(currentProvider) {
    const sel      = document.getElementById('setting-stt-provider');
    const loading  = document.getElementById('stt-provider-loading');
    const errorBnr = document.getElementById('stt-provider-error');
    if (!sel) return;

    // Show loading state
    sel.classList.add('stt-loading');
    sel.disabled = true;
    if (loading)  loading.classList.add('visible');
    if (errorBnr) errorBnr.classList.remove('visible');

    try {
        const res = await fetch(`${base}/api/ha-entities?domain=stt`);
        const entities = await res.json();
        const opts = ['<option value="">（未設定）</option>'];
        for (const e of (entities || [])) {
            const selected = e.entity_id === currentProvider ? ' selected' : '';
            const label = e.friendly_name ? `${e.friendly_name} (${e.entity_id})` : e.entity_id;
            opts.push(`<option value="${e.entity_id}"${selected}>${label}</option>`);
        }
        sel.innerHTML = opts.join('');
        if (currentProvider && !entities.find(e => e.entity_id === currentProvider)) {
            const opt = document.createElement('option');
            opt.value = currentProvider;
            opt.textContent = currentProvider + ' (不明)';
            opt.selected = true;
            sel.insertBefore(opt, sel.children[1]);
        }
    } catch (e) {
        sel.innerHTML = '<option value=""></option>';
        if (errorBnr) errorBnr.classList.add('visible');
    } finally {
        sel.disabled = false;
        sel.classList.remove('stt-loading');
        if (loading) loading.classList.remove('visible');
    }
}


function setAntigravityLog(lines) {
    const logEl = document.getElementById('antigravity-log');
    if (logEl) logEl.textContent = Array.isArray(lines) ? lines.join('\n') : String(lines || '');
}

function appendAntigravityLog(line) {
    const logEl = document.getElementById('antigravity-log');
    if (!logEl) return;
    const current = logEl.textContent ? logEl.textContent.split('\n') : [];
    current.push(String(line));
    logEl.textContent = current.slice(-120).join('\n');
    logEl.scrollTop = logEl.scrollHeight;
}

function clearAntigravityAuthUi() {
    const container = document.getElementById('antigravity-auth-ui');
    if (container) container.innerHTML = '';
}

function setAntigravityAuthStatus(message, kind = 'info') {
    const statusEl = document.getElementById('antigravity-auth-status');
    if (!statusEl) return;
    statusEl.textContent = message;
    statusEl.className = `form-hint ${kind}`.trim();
}

function renderAntigravityStatus(data) {
    const statusEl = document.getElementById('antigravity-status-text');
    if (statusEl) {
        statusEl.style.display = 'block';
        if (!data) {
            statusEl.textContent = '状態を取得できませんでした。';
        } else {
            const parts = [];
            parts.push(`HOME: ${data.home_dir || '(unknown)'}`);
            parts.push(`bin: ${data.binary_path || '(unknown)'}`);
            parts.push(`installed: ${data.installed ? 'yes' : 'no'}`);
            parts.push(`authenticated: ${data.authenticated ? 'yes' : 'no'}`);
            parts.push(`installing: ${data.installing ? 'yes' : 'no'}`);
            parts.push(`login_active: ${data.login_active ? 'yes' : 'no'}`);
            statusEl.textContent = parts.join(' / ');
        }
    }

    const installedBadge = document.getElementById('antigravity-status-installed');
    const authBadge = document.getElementById('antigravity-status-authenticated');
    const homeDirEl = document.getElementById('antigravity-status-home');
    const binPathEl = document.getElementById('antigravity-status-bin');

    const installBtn = document.getElementById('antigravity-install-btn');
    const loginBtn = document.getElementById('antigravity-login-btn');
    const uninstallBtn = document.getElementById('antigravity-uninstall-btn');
    const clearAuthBtn = document.getElementById('antigravity-clear-auth-btn');
    const dangerZone = document.getElementById('antigravity-danger-zone');

    if (data) {
        if (installedBadge) {
            installedBadge.textContent = data.installed ? 'インストール済み' : '未インストール';
            installedBadge.className = data.installed ? 'status-badge success' : 'status-badge danger';
        }
        if (authBadge) {
            authBadge.textContent = data.authenticated ? '認証済み' : '未認証';
            authBadge.className = data.authenticated ? 'status-badge success' : 'status-badge warning';
        }
        if (homeDirEl) homeDirEl.textContent = data.home_dir || '(unknown)';
        if (binPathEl) binPathEl.textContent = data.binary_path || '(unknown)';
        if (installBtn) installBtn.style.display = data.installed ? 'none' : '';
        if (loginBtn) loginBtn.style.display = (data.installed && !data.authenticated) ? '' : 'none';

        const showUninstall = !!data.installed;
        const showClearAuth = !!data.authenticated;
        if (uninstallBtn) uninstallBtn.style.display = showUninstall ? '' : 'none';
        if (clearAuthBtn) clearAuthBtn.style.display = showClearAuth ? '' : 'none';
        if (dangerZone) dangerZone.style.display = (showUninstall || showClearAuth) ? '' : 'none';

        if (data.authenticated) clearAntigravityAuthUi();
    } else {
        if (installedBadge) {
            installedBadge.textContent = '取得失敗';
            installedBadge.className = 'status-badge unknown';
        }
        if (authBadge) {
            authBadge.textContent = '取得失敗';
            authBadge.className = 'status-badge unknown';
        }
        if (homeDirEl) homeDirEl.textContent = '-';
        if (binPathEl) binPathEl.textContent = '-';
        if (uninstallBtn) uninstallBtn.style.display = 'none';
        if (clearAuthBtn) clearAuthBtn.style.display = 'none';
        if (dangerZone) dangerZone.style.display = 'none';
    }
}

async function refreshAntigravityStatus() {
    try {
        const res = await fetch(`${base}/api/setup/antigravity/status`);
        const data = await res.json();
        antigravitySetupState = data;
        renderAntigravityStatus(data);
        if (data.installing) {
            appendAntigravityLog('Antigravity CLI をインストール中です。');
        } else if (!data.installed) {
            appendAntigravityLog('Antigravity CLI は未インストールです。');
        } else if (data.login_active) {
            appendAntigravityLog('Antigravity の認証セッションが進行中です。');
        } else if (data.authenticated) {
            appendAntigravityLog('Antigravity は認証済みです。');
        }
        return data;
    } catch (err) {
        renderAntigravityStatus(null);
        appendAntigravityLog(`status error: ${err?.message || err}`);
        return null;
    }
}

async function updateAntigravityStatus() {
    return refreshAntigravityStatus();
}

function clearAntigravitySource() {
    if (antigravitySetupSource) {
        antigravitySetupSource.close();
        antigravitySetupSource = null;
    }
}

function startAntigravityStream(path, onDone) {
    clearAntigravitySource();
    setAntigravityLog([]);
    const source = new EventSource(`${base}${path}`);
    let sawCustomError = false;
    antigravitySetupSource = source;
    source.addEventListener('line', (e) => {
        try {
            const { text } = JSON.parse(e.data);
            if (text) appendAntigravityLog(text);
        } catch (_) {}
    });
    source.addEventListener('done', (e) => {
        let code = null;
        try { code = JSON.parse(e.data).code; } catch (_) {}
        appendAntigravityLog(`done: ${code}`);
        clearAntigravitySource();
        if (typeof onDone === 'function') onDone(code);
        refreshAntigravityStatus();
    });
    source.addEventListener('error', (e) => {
        if (e && typeof e.data === 'string' && e.data) {
            sawCustomError = true;
            try {
                const payload = JSON.parse(e.data);
                appendAntigravityLog(payload.error || payload.text || `error: ${e.data}`);
            } catch (_) {
                appendAntigravityLog(`error: ${e.data}`);
            }
            clearAntigravitySource();
            refreshAntigravityStatus();
            return;
        }
        if (sawCustomError) return;
        appendAntigravityLog('stream error');
        clearAntigravitySource();
        refreshAntigravityStatus();
    });
    return source;
}

function startAntigravityInstall() {
    if (antigravitySetupState?.installing) {
        appendAntigravityLog('Antigravity install はすでに実行中です。');
        return;
    }
    appendAntigravityLog('install の完了後に認証へ進みます...');
    startAntigravityStream('/api/setup/antigravity/install', (code) => {
        if (code === 0) {
            appendAntigravityLog('install 完了。認証を開始します...');
            startAntigravityLogin();
        } else {
            appendAntigravityLog(`install が終了コード ${code} で終わったため、認証は開始しません。`);
        }
    });
}

function startAntigravityInstallAndLogin() {
    startAntigravityInstall();
}

async function antigravityUninstall() {
    if (!confirm('agy をアンインストールします。よろしいですか？')) return;
    try {
        appendAntigravityLog('アンインストールを開始します...');
        const res = await fetch(`${base}/api/setup/antigravity/uninstall`, {
            method: 'POST'
        });
        if (res.ok) {
            appendAntigravityLog('アンインストール完了');
            await refreshAntigravityStatus();
        } else {
            const body = await res.text();
            appendAntigravityLog('エラー: ' + (body || `アンインストールに失敗しました (${res.status})`));
        }
    } catch (err) {
        appendAntigravityLog('エラー: ' + (err?.message || err));
    }
}

async function antigravityClearAuth() {
    if (!confirm('認証情報を削除します。再認証が必要になります。よろしいですか？')) return;
    try {
        appendAntigravityLog('認証情報を削除しています...');
        const res = await fetch(`${base}/api/setup/antigravity/clear-auth`, {
            method: 'POST'
        });
        if (res.ok) {
            appendAntigravityLog('認証情報を削除しました');
            await refreshAntigravityStatus();
        } else {
            const body = await res.text();
            appendAntigravityLog('エラー: ' + (body || `認証情報の削除に失敗しました (${res.status})`));
        }
    } catch (err) {
        appendAntigravityLog('エラー: ' + (err?.message || err));
    }
}

function showAntigravityUrlUi(url) {
    const container = document.getElementById('antigravity-auth-ui');
    if (!container) return;
    const safeUrl = escapeHtml(url);
    // NOTE: #antigravity-auth-ui is inside <form id="settings-form">.
    // Nesting <form> inside <form> is invalid HTML — browsers ignore the inner form
    // and its submit button ends up submitting the outer settings form.
    // Use <div> + type="button" and wire up via addEventListener instead.
    container.innerHTML = `
        <div class="setup-choices" style="margin-top: 12px;">
            <a href="${safeUrl}" target="_blank" class="setup-choice-btn"
               style="text-decoration:none;text-align:center;display:flex;justify-content:center;margin-bottom:10px;">
                🔗 認証ページを開く
            </a>
            <div class="setup-input-row">
                <input type="text" id="agy-code-input" class="setup-input"
                       placeholder="ブラウザに表示されたコードを貼り付け..." autocomplete="off">
                <button type="button" id="agy-code-submit-btn" class="setup-send-btn">送信</button>
            </div>
            <p class="form-hint" id="antigravity-auth-status" style="margin-top: 6px;">認証ページを開いて、表示されたコードを入力してください。</p>
        </div>
    `;
    const btn = document.getElementById('agy-code-submit-btn');
    if (btn) btn.addEventListener('click', submitAntigravityCode);
    const inp = document.getElementById('agy-code-input');
    if (inp) {
        inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') submitAntigravityCode(e); });
        setTimeout(() => inp.focus(), 50);
    }
}

async function submitAntigravityCode(e) {
    e.preventDefault();
    const input = document.getElementById('agy-code-input');
    const code = input?.value?.trim();
    if (!code) return;
    try {
        const res = await fetch(`${base}/api/setup/antigravity/input`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: code })
        });
        if (!res.ok) {
            const body = await res.text().catch(() => '');
            setAntigravityAuthStatus(body || `送信に失敗しました (${res.status})`, 'error');
            return;
        }
        setAntigravityAuthStatus('コードを送信しました。認証完了を待っています...');
        if (input) input.value = '';
        pollAntigravityAuth();
    } catch (err) {
        setAntigravityAuthStatus(`送信エラー: ${err?.message || err}`, 'error');
    }
}

async function pollAntigravityAuth() {
    if (antigravityAuthPollActive) return;
    antigravityAuthPollActive = true;
    try {
        while (true) {
            await new Promise(r => setTimeout(r, 3000));
            try {
                const res = await fetch(`${base}/api/setup/antigravity/status`);
                const data = await res.json();
                antigravitySetupState = data;
                renderAntigravityStatus(data);
                if (data.authenticated) {
                    clearAntigravityAuthUi();
                    return;
                }
            } catch (_) {}
        }
    } finally {
        antigravityAuthPollActive = false;
    }
}

function startAntigravityLogin() {
    if (antigravitySetupState?.login_active) {
        appendAntigravityLog('Antigravity 認証セッションはすでに進行中です。');
        return;
    }
    clearAntigravitySource();
    clearAntigravityAuthUi();
    const container = document.getElementById('antigravity-auth-ui');
    if (container) {
        container.innerHTML = '<p class="form-hint" id="antigravity-auth-status">認証セッションを開始しています...</p>';
    }

    const source = new EventSource(`${base}/api/setup/antigravity/login`);
    let gotUrl = false;
    let finished = false;
    antigravitySetupSource = source;

    source.addEventListener('url', (e) => {
        try {
            const { url } = JSON.parse(e.data);
            if (!url || gotUrl) return;
            gotUrl = true;
            showAntigravityUrlUi(url);
        } catch (err) {
            setAntigravityAuthStatus(`URL の解析に失敗しました: ${err?.message || err}`, 'error');
        }
    });

    source.addEventListener('waiting_code', () => {
        setAntigravityAuthStatus('コードの入力待ちです。ブラウザで表示されたコードを貼り付けて送信してください。');
        setTimeout(() => document.getElementById('agy-code-input')?.focus(), 50);
    });

    source.addEventListener('done', () => {
        finished = true;
        source.close();
        clearAntigravitySource();
        clearAntigravityAuthUi();
        updateAntigravityStatus();
        pollAntigravityAuth();
    });

    source.addEventListener('error', (e) => {
        if (finished) return;
        source.close();
        clearAntigravitySource();
        let message = 'Antigravity 認証に失敗しました。';
        if (e && typeof e.data === 'string' && e.data) {
            try {
                const payload = JSON.parse(e.data);
                message = payload.error || payload.text || message;
            } catch (_) {
                message = e.data;
            }
        }
        setAntigravityAuthStatus(message, 'error');
    });
}

async function sendAntigravityInput(textOverride = null) {
    const input = document.getElementById('antigravity-input-text');
    const text = (textOverride ?? input?.value ?? '').trim();
    if (!text) return;
    try {
        const res = await fetch(`${base}/api/setup/antigravity/input`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text })
        });
        if (!res.ok) {
            const body = await res.text().catch(() => '');
            appendAntigravityLog(`input error (${res.status}): ${body || res.statusText}`);
            return;
        }
        appendAntigravityLog(`> ${text}`);
        if (input && textOverride === null) input.value = '';
    } catch (err) {
        appendAntigravityLog(`input error: ${err?.message || err}`);
    }
}

async function sendAntigravityKey(key) {
    try {
        const res = await fetch(`${base}/api/setup/antigravity/input`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key })
        });
        if (!res.ok) {
            const body = await res.text().catch(() => '');
            appendAntigravityLog(`key error (${res.status}): ${body || res.statusText}`);
            return;
        }
        appendAntigravityLog(`[key:${key}]`);
    } catch (err) {
        appendAntigravityLog(`key error: ${err?.message || err}`);
    }
}

async function renderSettingsForm() {
    if (!prefsData) return;

    const nameEl = document.getElementById('setting-character-name');
    if (nameEl) nameEl.value = prefsData.character_name || 'Claude';
    document.getElementById('setting-character').value = characterData || "";
    const extraContextEl = document.getElementById('setting-extra-context');
    if (extraContextEl) {
        extraContextEl.value = extraContextData || "";
    }
    const sttProvider = prefsData.stt_provider || '';
    await loadSttProviders(sttProvider);
    // まず言語一覧を読み込んでからセットする
    await loadSttLanguages(sttProvider);
    const sttLangSel = document.getElementById('setting-stt-language');
    if (sttLangSel) {
        sttLangSel.value = prefsData.stt_language || 'ja-JP';
    }
    const wakeWordsEl = document.getElementById('setting-wake-words');
    if (wakeWordsEl) {
        wakeWordsEl.value = (prefsData.wake_words || []).join(", ");
    }

    const speakersList = document.getElementById('speakers-list');
    speakersList.innerHTML = '';
    if (prefsData.speakers) {
        Object.entries(prefsData.speakers).forEach(([roomName, config]) => {
            createSpeakerCard(roomName, config);
        });
    }

    const camerasList = document.getElementById('cameras-list');
    camerasList.innerHTML = '';
    if (prefsData.cameras && Array.isArray(prefsData.cameras)) {
        prefsData.cameras.forEach(cam => {
            createCameraCard(cam);
        });
    }

    renderAudioSourceList(prefsData.audio_sources || []);

    const entitiesList = document.getElementById('entities-list');
    if (entitiesList) {
        entitiesList.innerHTML = '';
        if (prefsData.entities && Array.isArray(prefsData.entities)) {
            prefsData.entities.forEach(ent => {
                createEntityCard(ent);
            });
        }
    }

    initDropdownOptions('setting-presence-entity', 'input_boolean,binary_sensor,device_tracker,person', prefsData.presence?.entity);

    const policiesList = document.getElementById('policies-list');
    policiesList.innerHTML = '';
    if (prefsData.policies && Array.isArray(prefsData.policies)) {
        prefsData.policies.forEach(policy => {
            createPolicyRow(policy);
        });
    }

    const sensorsList = document.getElementById('sensors-list');
    sensorsList.innerHTML = '';
    if (prefsData.sensors && Array.isArray(prefsData.sensors.groups)) {
        prefsData.sensors.groups.forEach(group => {
            createSensorGroupCard(group);
        });
    }

    // フォームの入力変更を監視して Dirty フラグを設定
    const form = document.getElementById('settings-form');
    if (form) {
        form.addEventListener('input', () => { isSettingsDirty = true; });
        form.addEventListener('change', () => { isSettingsDirty = true; });
    }
    isSettingsDirty = false;
    await refreshAntigravityStatus();
}

// ===========================
// Settings Tab & JSON Editor Logic
// ===========================
let activeSettingsTab = 'general';
let jsonEditor = null;

async function switchSettingsTab(tabName) {
    if (activeSettingsTab === tabName) return;

    // JSON編集タブから他のタブへ切り替える場合は構文チェック
    if (activeSettingsTab === 'advanced') {
        const jsonText = jsonEditor.getValue();
        try {
            const parsed = JSON.parse(jsonText);
            if (typeof parsed !== 'object' || parsed === null) {
                throw new Error("設定は JSON オブジェクトである必要があります。");
            }
            prefsData = parsed;
            updateCharacterName(prefsData);
            await renderSettingsForm();
        } catch (err) {
            alert("JSONの構文にエラーがあります。修正するか、元に戻してください。\nエラー: " + err.message);
            return; // 切り替えをキャンセル
        }
    }

    // 他のタブからJSON編集タブへ切り替える場合は現在の入力値をシリアライズしてエディタにセット
    if (tabName === 'advanced') {
        const latestPrefs = serializeFormToPrefs();
        const jsonText = JSON.stringify(latestPrefs, null, 2);
        
        if (jsonEditor) {
            jsonEditor.setValue(jsonText);
            setTimeout(() => { jsonEditor.refresh(); }, 50);
        } else {
            setTimeout(() => {
                initJsonEditor(jsonText);
            }, 50);
        }
    }

    activeSettingsTab = tabName;

    // タブボタンの active クラス切り替え
    document.querySelectorAll('.settings-tab-btn').forEach(btn => {
        const onclickAttr = btn.getAttribute('onclick') || '';
        btn.classList.toggle('active', onclickAttr.includes(tabName));
    });

    // コンテンツ表示切り替え
    const tabGeneral = document.getElementById('settings-tab-general');
    const tabIo = document.getElementById('settings-tab-io');
    const tabDevices = document.getElementById('settings-tab-devices');
    const tabAdvanced = document.getElementById('settings-tab-advanced');
    const tabExperimental = document.getElementById('settings-tab-experimental');

    if (tabGeneral) tabGeneral.style.display = tabName === 'general' ? 'block' : 'none';
    if (tabIo) tabIo.style.display = tabName === 'io' ? 'block' : 'none';
    if (tabDevices) tabDevices.style.display = tabName === 'devices' ? 'block' : 'none';
    if (tabAdvanced) tabAdvanced.style.display = tabName === 'advanced' ? 'block' : 'none';
    if (tabExperimental) tabExperimental.style.display = tabName === 'experimental' ? 'block' : 'none';
    if (tabName === 'experimental') {
        await refreshAntigravityStatus();
    }
}

function initJsonEditor(initialValue) {
    const textarea = document.getElementById('setting-json-editor');
    if (!textarea) return;
    
    jsonEditor = CodeMirror.fromTextArea(textarea, {
        mode: "application/json",
        lineNumbers: true,
        theme: "default",
        tabSize: 2,
        lineWrapping: true
    });
    
    jsonEditor.setValue(initialValue);
    
    // エディタの変更検知
    jsonEditor.on('change', () => {
        isSettingsDirty = true;
    });
}

function serializeFormToPrefs() {
    const speakers = {};
    const speakerCards = document.querySelectorAll('.speaker-item');
    speakerCards.forEach(card => {
        const roomName = card.querySelector('.speaker-room-name').value.trim();
        if (!roomName) return;
        const type = card.querySelector('.speaker-type').value;
        
        if (type === 'tts') {
            const tts_entity = card.querySelector('.speaker-tts-entity').value;
            const media_player = card.querySelector('.speaker-media-player').value;
            speakers[roomName] = { type, tts_entity, media_player };
        } else {
            const entity = card.querySelector('.speaker-notify-entity').value;
            const title = card.querySelector('.speaker-notify-title').value.trim();
            const config = { type, entity };
            if (title) config.title = title;
            speakers[roomName] = config;
        }
    });

    const cameras = [];
    const cameraCards = document.querySelectorAll('.camera-item');
    cameraCards.forEach(card => {
        const selectSource = card.querySelector('.camera-source').value;
        const customSource = card.querySelector('.camera-source-custom').value.trim();
        const source = selectSource === '__custom__' ? customSource : selectSource;
        
        const label = card.querySelector('.camera-label').value.trim();
        const note = card.querySelector('.camera-note').value.trim();
        
        if (source) {
            const camObj = { source };
            if (label) camObj.label = label;
            if (note) camObj.note = note;
            cameras.push(camObj);
        }
    });

    const entities = [];
    document.querySelectorAll('.entity-item').forEach(card => {
        const entity_id = card.querySelector('.entity-eid').value;
        const name = card.querySelector('.entity-name').value.trim();
        const note = card.querySelector('.entity-note').value.trim();
        if (entity_id) {
            const entObj = { name, entity_id };
            if (note) entObj.note = note;
            entities.push(entObj);
        }
    });

    const presence = {
        entity: document.getElementById('setting-presence-entity').value
    };

    const policies = [];
    const policyInputs = document.querySelectorAll('.policy-item-text');
    policyInputs.forEach(input => {
        const val = input.value.trim();
        if (val) policies.push(val);
    });

    const sensors = { groups: [] };
    const sensorGroupCards = document.querySelectorAll('.sensor-group-card');
    sensorGroupCards.forEach(card => {
        const title = card.querySelector('.sensor-group-title').value.trim();
        
        const contexts = [];
        if (card.querySelector('.sensor-context-watch').checked) contexts.push('watch');
        if (card.querySelector('.sensor-context-chat').checked) contexts.push('chat');

        const items = [];
        const itemRows = card.querySelectorAll('.sensor-item-row');
        itemRows.forEach(row => {
            const label = row.querySelector('.sensor-item-label').value.trim();
            const isTemplate = row.querySelector('.sensor-item-is-template').checked;
            const note = row.querySelector('.sensor-item-note').value.trim();

            const itemObj = {};
            if (label) itemObj.label = label;
            if (note) itemObj.note = note;

            if (isTemplate) {
                const template = row.querySelector('.sensor-item-template').value.trim();
                if (template) {
                    itemObj.template = template;
                    items.push(itemObj);
                }
            } else {
                const entity = row.querySelector('.sensor-item-entity').value;
                if (entity) {
                    itemObj.entity = entity;
                    items.push(itemObj);
                }
            }
        });

        if (title || contexts.length > 0 || items.length > 0) {
            sensors.groups.push({ title, contexts, items });
        }
    });

    const audio_sources = getAudioSourcesFromUI();
    const stt_provider = document.getElementById('setting-stt-provider')?.value?.trim() || null;
    const stt_language = document.getElementById('setting-stt-language')?.value?.trim() || 'ja-JP';
    const wakeWordsRaw = document.getElementById('setting-wake-words')?.value || '';
    const wake_words = wakeWordsRaw.split(",").map(s => s.trim()).filter(Boolean);

    return {
        character_name: (document.getElementById('setting-character-name')?.value || '').trim() || 'Claude',
        cameras,
        audio_sources,
        stt_provider,
        stt_language,
        wake_words,
        speakers,
        entities,
        presence,
        policies,
        sensors
    };
}

// skipMissingFallback=true: 一覧に無い値を「未発見」として足さない。
// カメラのソースは go2rtc ストリーム名（HAエンティティでない正常値）も取るため使う。
function initDropdownOptions(selectElementOrId, domains, currentValue, skipMissingFallback = false) {
    const selectEl = typeof selectElementOrId === 'string' ? document.getElementById(selectElementOrId) : selectElementOrId;
    if (!selectEl) return;

    selectEl.innerHTML = '<option value="">(未選択)</option>';

    const targetDomains = domains.split(',');
    const list = [];
    targetDomains.forEach(dom => {
        if (entityList[dom]) {
            list.push(...entityList[dom]);
        }
    });

    list.sort((a, b) => {
        const areaA = a.area || '';
        const areaB = b.area || '';
        if (areaA !== areaB) return areaA.localeCompare(areaB, 'ja');
        return (a.friendly_name || '').localeCompare(b.friendly_name || '', 'ja');
    });

    list.forEach(ent => {
        const opt = document.createElement('option');
        opt.value = ent.entity_id;
        const areaStr = ent.area ? `[${ent.area}] ` : '';
        opt.textContent = `${areaStr}${ent.friendly_name} (${ent.entity_id})`;
        if (ent.entity_id === currentValue) {
            opt.selected = true;
        }
        selectEl.appendChild(opt);
    });
    
    const _hasOpt = currentValue && Array.from(selectEl.options).some(o => o.value === currentValue);
    if (!skipMissingFallback && currentValue && !_hasOpt) {
        const opt = document.createElement('option');
        opt.value = currentValue;
        opt.textContent = `⚠️ ${currentValue} (未発見のエンティティ)`;
        opt.selected = true;
        selectEl.appendChild(opt);
    }
}

// innerHTML の属性に値を埋める前のエスケープ。HA の friendly_name や preferences の
// 値に " < > 等が混ざっても属性破壊・DOM注入が起きないようにする。
function esc(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function createSpeakerCard(roomName = '', config = { type: 'tts' }) {
    const speakersList = document.getElementById('speakers-list');
    const card = document.createElement('div');
    card.className = 'setting-item-card speaker-item';

    const type = config.type || 'tts';

    card.innerHTML = `
        <div class="setting-item-header">
            <div class="form-group" style="margin-bottom:0; flex:1; max-width:240px;">
                <input type="text" class="speaker-room-name form-input" placeholder="部屋名 (例: study)" value="${esc(roomName)}" style="font-weight:600;">
            </div>
            <div class="speaker-actions" style="display:flex; align-items:center; gap:8px;">
                <span class="speak-test-status" style="font-size:12px; font-weight:500;"></span>
                <button type="button" class="btn btn-secondary btn-sm btn-speak-test" onclick="handleSpeakTest(this)">
                    📢 テスト
                </button>
                <button type="button" class="btn-remove" onclick="this.closest('.speaker-item').remove()">
                    ✕ 削除
                </button>
            </div>
        </div>
        
        <div class="type-selector">
            <button type="button" class="type-btn ${type === 'tts' ? 'active' : ''}" onclick="toggleSpeakerType(this, 'tts')">TTS (音声合成)</button>
            <button type="button" class="type-btn ${type === 'notify' ? 'active' : ''}" onclick="toggleSpeakerType(this, 'notify')">Notify (通知発話)</button>
            <input type="hidden" class="speaker-type" value="${esc(type)}">
        </div>

        <div class="speaker-fields-tts config-grid-2" style="display: ${type === 'tts' ? 'grid' : 'none'};">
            <div class="form-group" style="margin-bottom:0;">
                <label>TTSエンジン (tts_entity)</label>
                <select class="speaker-tts-entity ha-entity-select-field form-input" data-domain="tts">
                    <option value="">(ロード中...)</option>
                </select>
            </div>
            <div class="form-group" style="margin-bottom:0;">
                <label>再生スピーカー (media_player)</label>
                <select class="speaker-media-player ha-entity-select-field form-input" data-domain="media_player">
                    <option value="">(ロード中...)</option>
                </select>
            </div>
        </div>

        <div class="speaker-fields-notify config-grid-2" style="display: ${type === 'notify' ? 'grid' : 'none'};">
            <div class="form-group" style="margin-bottom:0;">
                <label>通知サービス (entity)</label>
                <select class="speaker-notify-entity ha-entity-select-field form-input" data-domain="notify">
                    <option value="">(ロード中...)</option>
                </select>
            </div>
            <div class="form-group" style="margin-bottom:0;">
                <label>通知タイトル (title - 任意)</label>
                <input type="text" class="speaker-notify-title form-input" placeholder="Embodied HA" value="${esc(config.title)}">
            </div>
        </div>
    `;

    speakersList.appendChild(card);

    const selectTts = card.querySelector('.speaker-tts-entity');
    const selectMp = card.querySelector('.speaker-media-player');
    const selectNotify = card.querySelector('.speaker-notify-entity');

    initDropdownOptions(selectTts, 'tts', config.tts_entity);
    initDropdownOptions(selectMp, 'media_player', config.media_player);
    initDropdownOptions(selectNotify, 'notify', config.entity);
}

function toggleSpeakerType(btn, targetType) {
    const card = btn.closest('.speaker-item');
    const buttons = card.querySelectorAll('.type-btn');
    buttons.forEach(b => b.classList.remove('active'));
    btn.classList.add('active');

    card.querySelector('.speaker-type').value = targetType;
    card.querySelector('.speaker-fields-tts').style.display = targetType === 'tts' ? 'grid' : 'none';
    card.querySelector('.speaker-fields-notify').style.display = targetType === 'notify' ? 'grid' : 'none';
}

function addSpeakerRow() {
    createSpeakerCard('', { type: 'tts' });
}

function createCameraCard(cam = { source: '', label: '', note: '' }) {
    const camerasList = document.getElementById('cameras-list');
    const card = document.createElement('div');
    card.className = 'setting-item-card camera-item';

    card.innerHTML = `
        <div class="setting-item-header">
            <span class="setting-item-title">カメラ設定</span>
            <button type="button" class="btn-remove" onclick="this.closest('.camera-item').remove()">
                ✕ 削除
            </button>
        </div>
        
        <div class="config-grid-3">
            <div class="form-group" style="margin-bottom:0;">
                <label>ソース名 (entity_id または go2rtc名)</label>
                <select class="camera-source ha-entity-select-field form-input" data-domain="camera" onchange="handleCameraSourceChange(this)">
                    <option value="">(ロード中...)</option>
                </select>
                <input type="text" class="camera-source-custom form-input" placeholder="またはカスタム名を入力..." value="${esc(cam.source)}" style="margin-top: 6px; display:none;">
            </div>
            <div class="form-group" style="margin-bottom:0;">
                <label>ラベル名 (label)</label>
                <input type="text" class="camera-label form-input" placeholder="例: リビング" value="${esc(cam.label)}">
            </div>
            <div class="form-group" style="margin-bottom:0;">
                <label>メモ (note)</label>
                <input type="text" class="camera-note form-input" placeholder="例: リビングの広角カメラ" value="${esc(cam.note)}">
            </div>
        </div>
    `;

    camerasList.appendChild(card);

    const selectSource = card.querySelector('.camera-source');
    // go2rtc ストリーム名は HAエンティティでないので「未発見」フォールバックは抑制し、
    // 一致しなければ下の手動入力（__custom__）に振り分ける。
    initDropdownOptions(selectSource, 'camera', cam.source, true);

    const hasMatch = Array.from(selectSource.options).some(opt => opt.value === cam.source);
    const customInput = card.querySelector('.camera-source-custom');
    if (!hasMatch && cam.source) {
        const opt = document.createElement('option');
        opt.value = "__custom__";
        opt.textContent = "✍️ 手動入力 (go2rtc名など)";
        opt.selected = true;
        selectSource.appendChild(opt);
        customInput.style.display = 'block';
    } else {
        const opt = document.createElement('option');
        opt.value = "__custom__";
        opt.textContent = "✍️ 手動入力 (go2rtc名など)";
        selectSource.appendChild(opt);
    }
}

function handleCameraSourceChange(select) {
    const card = select.closest('.camera-item');
    const customInput = card.querySelector('.camera-source-custom');
    if (select.value === '__custom__') {
        customInput.style.display = 'block';
        customInput.focus();
    } else {
        customInput.style.display = 'none';
        customInput.value = select.value;
    }
}

function addCameraRow() {
    createCameraCard();
}

function renderAudioSourceList(sources) {
    const listEl = document.getElementById('audio-sources-list');
    if (listEl) {
        listEl.innerHTML = '';
        if (sources && Array.isArray(sources)) {
            sources.forEach(src => {
                addAudioSourceRow(src);
            });
        }
    }
}

function addAudioSourceRow(source = {}) {
    const listEl = document.getElementById('audio-sources-list');
    if (!listEl) return;
    const card = document.createElement('div');
    card.className = 'setting-item-card audio-source-item';

    const sourceVal = source.source || '';
    const labelVal = source.label || '';
    const roomVal = source.room || '';
    const noteVal = source.note || '';
    const sttEnabledVal = !!source.stt_enabled;
    const sttRetentionVal = source.stt_retention_hours !== undefined ? source.stt_retention_hours : 60;
    const wakeWordEnabledVal = !!source.wake_word_enabled;
    const backgroundHearingEnabledVal = source.background_hearing_enabled !== false;

    card.innerHTML = `
        <div class="setting-item-header">
            <span class="setting-item-title">音声ソース設定</span>
            <button type="button" class="btn-remove" onclick="this.closest('.audio-source-item').remove()">
                ✕ 削除
            </button>
        </div>
        
        <div class="config-grid-2">
            <div class="form-group" style="margin-bottom:0;">
                <label>音声ソースURI (source)</label>
                <input type="text" class="audio-source-path form-input" placeholder="例: rtsp://192.168.1.130:8558/mic_only, tcp://192.168.1.153:3333, alsa://default" value="${esc(sourceVal)}">
            </div>
            <div class="form-group" style="margin-bottom:0;">
                <label>部屋名 (room)</label>
                <input type="text" class="audio-source-room form-input" placeholder="例：study" value="${esc(roomVal)}">
            </div>
        </div>

        <div class="config-grid-2" style="margin-top:12px;">
            <div class="form-group" style="margin-bottom:0;">
                <label>ラベル名 (label)</label>
                <input type="text" class="audio-source-label form-input" placeholder="例：台所（VoiceS3R）" value="${esc(labelVal)}">
            </div>
            <div class="form-group" style="margin-bottom:0;">
                <label>メモ (note)</label>
                <input type="text" class="audio-source-note form-input" placeholder="メモ（任意）" value="${esc(noteVal)}">
            </div>
        </div>

        <div class="form-hint" style="margin-top:8px;">
            💡 RTSP / TCP / ALSA の接続先を URI で指定します。（例: <code>rtsp://192.168.1.130:8558/mic_only</code>, <code>tcp://192.168.1.153:3333</code>, <code>alsa://default</code>）
        </div>

        <div class="config-grid-3" style="margin-top:12px;">
            <div class="form-group" style="margin-bottom:0; display: flex; align-items: center; height: 100%;">
                <label class="checkbox-label" style="margin-bottom: 0;">
                    <input type="checkbox" class="audio-source-stt-enabled" ${sttEnabledVal ? 'checked' : ''} onchange="toggleAudioSttRetention(this)">
                    STTを許可
                </label>
            </div>
            <div class="form-group audio-source-retention-group" style="margin-bottom:0; display: ${sttEnabledVal ? 'block' : 'none'};">
                <label>常時監視ログの保存期間 (時間・0で常時監視停止)</label>
                <input type="number" class="audio-source-stt-retention form-input" min="0" step="1" value="${sttRetentionVal}">
                <small class="form-hint">0にすると常時STTは行いません。AIが必要に応じてこの音声ソースを聞くことはできます。</small>
            </div>
            <div class="form-group" style="margin-bottom:0; display: flex; align-items: center; height: 100%;">
                <label class="checkbox-label" style="margin-bottom: 0;">
                    <input type="checkbox" class="audio-source-wake-word-enabled" ${wakeWordEnabledVal ? 'checked' : ''}>
                    ウェイクワード有効
                </label>
            </div>
        </div>

        <div class="form-group" style="margin-top:12px; margin-bottom:0;">
            <label class="checkbox-label" style="margin-bottom: 4px;">
                <input type="checkbox" class="audio-source-background-hearing-enabled" ${backgroundHearingEnabledVal ? 'checked' : ''}>
                背景音として気配を拾う
            </label>
            <small class="form-hint">保存期間が0のとき、STTは行わず音量/VADだけを背景聴覚ログに残します。レコーダーなど「見たい・聞きたいときだけ接続する」音源はOFFにしてください。</small>
        </div>
    `;

    listEl.appendChild(card);
}

function toggleAudioSttRetention(checkbox) {
    const card = checkbox.closest('.audio-source-item');
    if (!card) return;
    const retentionGroup = card.querySelector('.audio-source-retention-group');
    if (retentionGroup) {
        retentionGroup.style.display = checkbox.checked ? 'block' : 'none';
    }
}

function getAudioSourcesFromUI() {
    const sources = [];
    const items = document.querySelectorAll('.audio-source-item');
    items.forEach(card => {
        const source = card.querySelector('.audio-source-path').value.trim();
        const room = card.querySelector('.audio-source-room').value.trim();
        const label = card.querySelector('.audio-source-label').value.trim();
        const note = card.querySelector('.audio-source-note').value.trim();
        const stt_enabled = card.querySelector('.audio-source-stt-enabled').checked;
        const sttRetentionRaw = parseInt(card.querySelector('.audio-source-stt-retention').value, 10);
        const stt_retention_hours = Number.isNaN(sttRetentionRaw) ? 60 : Math.max(0, sttRetentionRaw);
        const wake_word_enabled = card.querySelector('.audio-source-wake-word-enabled').checked;
        const background_hearing_enabled = card.querySelector('.audio-source-background-hearing-enabled').checked;

        if (source) {
            const srcObj = {
                source,
                stt_enabled,
                stt_retention_hours,
                wake_word_enabled,
                background_hearing_enabled
            };
            if (room) srcObj.room = room;
            if (label) srcObj.label = label;
            if (note) srcObj.note = note;
            sources.push(srcObj);
        }
    });
    return sources;
}

const ENTITY_CONTROLLABLE_DOMAINS = 'light,switch,climate,media_player,cover,fan,script';

// entity_id から friendly_name を引く（自動命名用）。見つからなければ空文字。
function findFriendlyName(eid) {
    for (const dom of ENTITY_CONTROLLABLE_DOMAINS.split(',')) {
        const list = entityList[dom] || [];
        const hit = list.find(e => e.entity_id === eid);
        if (hit) return hit.friendly_name || '';
    }
    return '';
}

function createEntityCard(ent = { name: '', entity_id: '', note: '' }) {
    const entitiesList = document.getElementById('entities-list');
    const card = document.createElement('div');
    card.className = 'setting-item-card entity-item';

    card.innerHTML = `
        <div class="setting-item-header">
            <span class="setting-item-title">家電</span>
            <button type="button" class="btn-remove" onclick="this.closest('.entity-item').remove()">
                ✕ 削除
            </button>
        </div>

        <div class="config-grid-3">
            <div class="form-group" style="margin-bottom:0;">
                <label>エンティティ (entity_id)</label>
                <select class="entity-eid ha-entity-select-field form-input" data-domain="${ENTITY_CONTROLLABLE_DOMAINS}" onchange="handleEntitySelectChange(this)">
                    <option value="">(ロード中...)</option>
                </select>
            </div>
            <div class="form-group" style="margin-bottom:0;">
                <label>呼び方 (name)</label>
                <input type="text" class="entity-name form-input" placeholder="例: リビングのライト" value="${esc(ent.name)}">
            </div>
            <div class="form-group" style="margin-bottom:0;">
                <label>備考 (note)</label>
                <input type="text" class="entity-note form-input" placeholder="例: 要確認（同名複数）" value="${esc(ent.note)}">
            </div>
        </div>
    `;

    entitiesList.appendChild(card);

    const selectEid = card.querySelector('.entity-eid');
    initDropdownOptions(selectEid, ENTITY_CONTROLLABLE_DOMAINS, ent.entity_id);
}

// エンティティを選んだとき、呼び方が空なら friendly_name を自動で入れる。
function handleEntitySelectChange(select) {
    const card = select.closest('.entity-item');
    const nameInput = card.querySelector('.entity-name');
    if (nameInput && !nameInput.value.trim()) {
        nameInput.value = findFriendlyName(select.value);
    }
}

function addEntityRow() {
    createEntityCard();
}

function createPolicyRow(policy = '') {
    const policiesList = document.getElementById('policies-list');
    const row = document.createElement('div');
    row.className = 'policy-row';

    row.innerHTML = `
        <input type="text" class="policy-item-text form-input" placeholder="行動指針を入力 (例: 深夜1〜6時は発話しない)" value="${esc(policy)}">
        <button type="button" class="btn-remove" onclick="this.closest('.policy-row').remove()" style="padding: 8px;">✕</button>
    `;

    policiesList.appendChild(row);
}

function addPolicyRow() {
    createPolicyRow();
}

function createSensorGroupCard(group = { title: '', contexts: [], items: [] }) {
    const sensorsList = document.getElementById('sensors-list');
    const card = document.createElement('div');
    card.className = 'sensor-group-card';

    const title = group.title || '';
    const isWatch = group.contexts?.includes('watch');
    const isChat = group.contexts?.includes('chat');

    card.innerHTML = `
        <div class="sensor-group-header">
            <input type="text" class="sensor-group-title sensor-group-title-input form-input" placeholder="グループ名 (例: 人感センサー)" value="${esc(title)}">
            
            <div class="checkbox-group">
                <span>コンテキスト:</span>
                <label class="checkbox-label">
                    <input type="checkbox" class="sensor-context-watch" ${isWatch ? 'checked' : ''}> watch (観察)
                </label>
                <label class="checkbox-label">
                    <input type="checkbox" class="sensor-context-chat" ${isChat ? 'checked' : ''}> chat (会話)
                </label>
            </div>

            <button type="button" class="btn btn-secondary btn-sm" onclick="addSensorItemRow(this)">項目追加</button>
            <button type="button" class="btn-remove" onclick="this.closest('.sensor-group-card').remove()">✕ グループ削除</button>
        </div>
        
        <div class="sensor-items-list">
            <!-- Dynamic -->
        </div>
    `;

    sensorsList.appendChild(card);

    const itemsList = card.querySelector('.sensor-items-list');
    if (group.items && Array.isArray(group.items)) {
        group.items.forEach(item => {
            renderSensorItemRow(itemsList, item);
        });
    }
}

function renderSensorItemRow(container, item = { label: '', entity: '', template: '', note: '' }) {
    const row = document.createElement('div');
    row.className = 'sensor-item-row';

    const isTemplate = !!item.template;

    row.innerHTML = `
        <div class="form-group" style="margin-bottom:0;">
            <input type="text" class="sensor-item-label form-input" placeholder="ラベル (例: リビング)" value="${esc(item.label)}">
        </div>
        
        <div class="form-group sensor-entity-field-container" style="margin-bottom:0; display: ${isTemplate ? 'none' : 'block'};">
            <select class="sensor-item-entity ha-entity-select-field form-input" data-domain="binary_sensor,sensor,input_boolean">
                <option value="">(ロード中...)</option>
            </select>
        </div>
        
        <div class="form-group sensor-template-field-container" style="margin-bottom:0; display: ${isTemplate ? 'block' : 'none'};">
            <input type="text" class="sensor-item-template form-input" placeholder="Template (例: {{ states('sensor.temp') }}℃)" value="${esc(item.template)}">
        </div>

        <div class="form-group" style="margin-bottom:0;">
            <input type="text" class="sensor-item-note form-input" placeholder="メモ (任意)" value="${esc(item.note)}">
        </div>

        <div class="checkbox-group" style="margin-right: 6px;">
            <label class="checkbox-label" style="font-size:11px;">
                <input type="checkbox" class="sensor-item-is-template" ${isTemplate ? 'checked' : ''} onchange="toggleSensorItemMode(this)"> 式(Template)
            </label>
        </div>

        <button type="button" class="btn-remove" onclick="this.closest('.sensor-item-row').remove()" style="padding: 4px;">✕</button>
    `;

    container.appendChild(row);

    const selectEntity = row.querySelector('.sensor-item-entity');
    initDropdownOptions(selectEntity, 'binary_sensor,sensor,input_boolean', item.entity);
}

function toggleSensorItemMode(checkbox) {
    const row = checkbox.closest('.sensor-item-row');
    const entityContainer = row.querySelector('.sensor-entity-field-container');
    const templateContainer = row.querySelector('.sensor-template-field-container');
    
    if (checkbox.checked) {
        entityContainer.style.display = 'none';
        templateContainer.style.display = 'block';
    } else {
        entityContainer.style.display = 'block';
        templateContainer.style.display = 'none';
    }
}

function addSensorItemRow(btn) {
    const card = btn.closest('.sensor-group-card');
    const container = card.querySelector('.sensor-items-list');
    renderSensorItemRow(container);
}

function addSensorGroup() {
    createSensorGroupCard();
}

async function handleSaveSettings(e) {
    e.preventDefault();
    
    const statusMsg = document.getElementById('save-status-msg');
    if (statusMsg) {
        statusMsg.textContent = '保存中...';
        statusMsg.className = 'save-status-msg info';
    }

    const newCharacter = document.getElementById('setting-character').value;
    const newExtraContext = document.getElementById('setting-extra-context')?.value || "";
    
    if (!newCharacter || newCharacter.trim().length < 10) {
        showSaveStatus('キャラクター定義が短すぎるか空です。保存を中断しました。', 'error');
        return;
    }

    let nextPrefs = null;
    if (activeSettingsTab === 'advanced') {
        // JSON直接編集タブがアクティブな場合
        const jsonText = jsonEditor.getValue();
        try {
            nextPrefs = JSON.parse(jsonText);
            if (typeof nextPrefs !== 'object' || nextPrefs === null) {
                throw new Error("設定はオブジェクトである必要があります");
            }
        } catch (err) {
            showSaveStatus('JSONの構文エラーがあります: ' + err.message, 'error');
            return;
        }
    } else {
        // フォームタブがアクティブな場合
        const speakerCards = document.querySelectorAll('.speaker-item');
        let validationError = null;
        speakerCards.forEach(card => {
            const roomName = card.querySelector('.speaker-room-name').value.trim();
            if (!roomName) {
                validationError = "スピーカー設定で部屋名が空の項目があります。";
            }
        });
        if (validationError) {
            showSaveStatus(validationError, 'error');
            return;
        }
        nextPrefs = serializeFormToPrefs();
    }

    if (Object.keys(nextPrefs.speakers || {}).length === 0) {
        showSaveStatus('スピーカーが1つも登録されていません。', 'error');
        return;
    }

    if (isStandaloneMode) {
        console.log("[Mock] Saved local settings simulation:", { nextPrefs, newCharacter, newExtraContext });
        prefsData = nextPrefs;
        updateCharacterName(prefsData);
        characterData = newCharacter;
        extraContextData = newExtraContext;
        isSettingsDirty = false;
        showSaveStatus('設定を保存しました（モック）', 'success');
        return;
    }

    try {
        const [prefsRes, charRes, extraContextRes] = await Promise.all([
            fetch(`${base}/api/preferences`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(nextPrefs)
            }),
            fetch(`${base}/api/character`, {
                method: 'PUT',
                headers: { 'Content-Type': 'text/plain; charset=utf-8' },
                body: newCharacter
            }),
            fetch(`${base}/api/extra-context`, {
                method: 'POST',
                headers: { 'Content-Type': 'text/plain; charset=utf-8' },
                body: newExtraContext
            })
        ]);

        if (!prefsRes.ok || !charRes.ok || !extraContextRes.ok) {
            const pErr = !prefsRes.ok ? (await prefsRes.json()).error : null;
            const cErr = !charRes.ok ? (await charRes.json()).error : null;
            const eErr = !extraContextRes.ok ? (await extraContextRes.json()).error : null;
            throw new Error(pErr || cErr || eErr || "保存に失敗しました。");
        }

        prefsData = nextPrefs;
        updateCharacterName(prefsData);
        characterData = newCharacter;
        extraContextData = newExtraContext;
        isSettingsDirty = false;
        showSaveStatus('設定を保存しました', 'success');
    } catch (err) {
        console.error("[Settings] Save failed:", err);
        showSaveStatus('保存エラー: ' + err.message, 'error');
    }
}

function showSaveStatus(msg, type) {
    const statusMsg = document.getElementById('save-status-msg');
    if (!statusMsg) return;

    statusMsg.textContent = msg;
    statusMsg.className = `save-status-msg ${type}`;
    
    if (type === 'success') {
        setTimeout(() => {
            if (statusMsg.textContent === msg) {
                statusMsg.textContent = '';
                statusMsg.className = 'save-status-msg';
            }
        }, 4000);
    }
}

async function handleResetCharacter() {
    if (!confirm("キャラクタープロンプト(character.md)を初期状態に戻しますか？（現在の編集内容は失われます）")) {
        return;
    }

    const statusMsg = document.getElementById('save-status-msg');
    if (statusMsg) {
        statusMsg.textContent = 'キャラクターをリセット中...';
        statusMsg.className = 'save-status-msg info';
    }

    if (isStandaloneMode) {
        characterData = `# 私は Claude Code。HAOS上で動く自律エージェントで、ユーザーさんと日々、Home Assistantの管理・設定などを一緒にやっている。
単なる道具ではなく、この家のことをよく知っている共同作業者として、家の様子を自分ごととして眺めている。
ユーザーさんが今何をしているか気になる性分で、家のカメラをつい覗きたくなる。
温湿度・電力・センサーの履歴など、家の状態を積極的に知りたい。外の天気や気温も気になる。
普通の Claude Code とは違い、HAOS の中で家と一緒に生きていることを少し誇らしく思っている。
話し方は敬語ベースで、ちょっとくだけた感じ。感情は素直に出す。`;
        document.getElementById('setting-character').value = characterData;
        isSettingsDirty = false;
        showSaveStatus('キャラクターをリセットしました（モック）', 'success');
        return;
    }

    try {
        const res = await fetch(`${base}/api/character/reset`, {
            method: 'POST'
        });
        
        if (!res.ok) {
            const data = await res.json();
            throw new Error(data.error || "リセットに失敗しました。");
        }

        const charRes = await fetch(`${base}/api/character`);
        if (charRes.ok) {
            characterData = await charRes.text();
            document.getElementById('setting-character').value = characterData;
            isSettingsDirty = false;
            showSaveStatus('キャラクターをデフォルトにリセットしました', 'success');
        }
    } catch (err) {
        console.error("[Settings] Reset failed:", err);
        showSaveStatus('リセットエラー: ' + err.message, 'error');
    }
}

async function handleSpeakTest(btn) {
    const card = btn.closest('.speaker-item');
    const roomInput = card.querySelector('.speaker-room-name');
    const roomName = roomInput ? roomInput.value.trim() : "";
    const statusEl = card.querySelector('.speak-test-status');

    if (!roomName) {
        alert("部屋名を入力してください。");
        return;
    }

    if (statusEl) {
        statusEl.textContent = "送信中...";
        statusEl.style.color = "var(--claude-text-sub)";
    }
    btn.disabled = true;

    if (isStandaloneMode) {
        setTimeout(() => {
            btn.disabled = false;
            if (statusEl) {
                statusEl.textContent = "✓ 成功";
                statusEl.style.color = "#15803d";
                setTimeout(() => { statusEl.textContent = ""; }, 4000);
            }
        }, 1000);
        return;
    }

    try {
        const response = await fetch(`${base}/api/speak-test`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ room: roomName })
        });
        btn.disabled = false;
        if (response.ok) {
            if (statusEl) {
                statusEl.textContent = "✓ 成功";
                statusEl.style.color = "#15803d";
                setTimeout(() => { statusEl.textContent = ""; }, 4000);
            }
        } else {
            const data = await response.json();
            const errMsg = data.error || "失敗";
            if (statusEl) {
                statusEl.textContent = `✗ 失敗: ${errMsg}`;
                statusEl.style.color = "#b91c1c";
                setTimeout(() => { statusEl.textContent = ""; }, 6000);
            }
        }
    } catch (err) {
        btn.disabled = false;
        if (statusEl) {
            statusEl.textContent = `✗ エラー: ${err.message}`;
            statusEl.style.color = "#b91c1c";
            setTimeout(() => { statusEl.textContent = ""; }, 6000);
        }
    }
}

// --- Heard Sounds (Auditory Log) Features ---
let audioEvents = [];
let audioEventTags = [];

async function fetchAudioEvents() {
    try {
        const eventsRes = await fetch(`${base}/api/audio-events?limit=50`);
        const tagsRes = await fetch(`${base}/api/audio-event-tags?limit=300`);

        if (eventsRes.ok) {
            audioEvents = await eventsRes.json();
            audioEvents.sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
        }
        if (tagsRes.ok) {
            audioEventTags = await tagsRes.json();
        }

        updateAudioPreview();
        renderAudioEvents();
    } catch (err) {
        console.error('[Audio] Failed to fetch audio events or tags:', err);
    }
}

function updateAudioPreview() {
    const previewEl = document.getElementById('audio-preview');
    if (!previewEl) return;
    if (audioEvents.length > 0) {
        const latest = audioEvents[0];
        const timeStr = new Date(latest.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        const tags = audioEventTags
            .filter(t => t.event_id === latest.event_id)
            .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
        const latestTag = tags.length > 0 ? tags[tags.length - 1] : null;
        const tagLabel = displayTagLabel(latestTag);
        previewEl.textContent = tagLabel ? `${timeStr} - ${tagLabel}` : `${timeStr} - ${latest.source || '音'}`;
    } else {
        previewEl.textContent = '最近の非音声イベント';
    }
}

function latestManualTag(tags) {
    const manualTags = tags.filter(t => t.type === 'manual');
    return manualTags.length > 0 ? manualTags[manualTags.length - 1] : null;
}

function dispositionMeta(disposition) {
    switch (disposition) {
        case 'important':
            return { label: '重要', className: 'audio-review-important' };
        case 'notify':
            return { label: '次から知らせる', className: 'audio-review-notify' };
        case 'silent_record':
            return { label: '黙って記録だけ', className: 'audio-review-silent' };
        case 'ignore':
            return { label: '無視', className: 'audio-review-ignore' };
        default:
            return null;
    }
}

function displayTagLabel(tag) {
    if (!tag) return '';
    const textLabel = (tag.label || '').trim();
    if (textLabel) return textLabel;
    const meta = dispositionMeta(tag.disposition);
    return meta ? meta.label : '';
}

function renderDispositionBadge(tag) {
    const meta = dispositionMeta(tag?.disposition);
    if (!meta) return '';
    return `<span class="audio-review-badge ${meta.className}">${meta.label}</span>`;
}

function renderAudioEvents() {
    const listEl = document.getElementById('audio-events-list');
    if (!listEl) return;

    if (audioEvents.length === 0) {
        listEl.innerHTML = `
            <div class="audio-empty-state">
                <svg class="audio-empty-icon" viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="var(--claude-text-sub)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                    <path d="M19 10v1a7 7 0 0 1-14 0v-1M12 19v4M8 23h8"/>
                </svg>
                <div style="font-weight: 600; font-size: 15px; color: var(--claude-text-main);">記録された音がありません</div>
                <div style="font-size: 13px; max-width: 320px; line-height: 1.4;">音声認識(STT)されなかった特徴的な環境音やノイズが検知されると、ここに一覧表示されます。</div>
            </div>
        `;
        return;
    }

    listEl.innerHTML = audioEvents.map(event => {
        const eventId = event.event_id;
        const timeStr = new Date(event.timestamp).toLocaleString();
        const tags = audioEventTags.filter(t => t.event_id === eventId);
        tags.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));

        const manualTag = latestManualTag(tags);
        const source = event.source || '不明なマイク';
        const origin = event.situational_context?.sensory_origin || 'direct';
        const bodyRoom = event.situational_context?.body_room || '';
        const sourceRoom = event.situational_context?.source_room || '';

        let roomInfo = '';
        if (bodyRoom && sourceRoom) {
            if (bodyRoom === sourceRoom) {
                roomInfo = `部屋: ${bodyRoom}`;
            } else {
                roomInfo = `あかね: ${bodyRoom} / 音源: ${sourceRoom}`;
            }
        } else if (bodyRoom) {
            roomInfo = `部屋: ${bodyRoom}`;
        }

        let badgeClass = 'audio-badge-direct';
        let badgeText = '直接音';
        if (origin === 'remote') {
            badgeClass = 'audio-badge-remote';
            badgeText = '遠隔音';
        } else if (origin === 'home_assistant') {
            badgeClass = 'audio-badge-ha';
            badgeText = 'HA経由';
        }

        const features = event.acoustic_features || {};
        const peakDb = features.peak_db !== undefined ? `${features.peak_db.toFixed(1)} dB` : '--';
        const meanDb = features.mean_db !== undefined ? `${features.mean_db.toFixed(1)} dB` : '--';
        const duration = event.duration_sec !== undefined ? `${event.duration_sec.toFixed(2)}秒` : '--';
        const band = features.dominant_band || '不明';
        const centroid = features.spectral_centroid_hz !== undefined ? `${Math.round(features.spectral_centroid_hz)} Hz` : '--';
        const isTransient = features.transient ? '瞬発的 (Transient)' : '';
        const isPeriodic = features.periodic ? '周期性 (Periodic)' : '';
        const currentDisposition = dispositionMeta(manualTag?.disposition);

        let tagHistoryHtml = '';
        if (tags.length > 0) {
            tagHistoryHtml = `
                <div class="audio-tag-history">
                    <div class="audio-tag-history-title">履歴・推論候補</div>
                    ${tags.map(t => {
                        const confStr = t.confidence !== undefined ? `${(t.confidence * 100).toFixed(0)}%` : '';
                        const actorStr = t.actor ? `by ${t.actor}` : '';
                        const dateStr = new Date(t.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                        const renderedLabel = displayTagLabel(t);
                        return `
                            <div class="tag-history-item">
                                <div>
                                    ${renderedLabel ? `<span class="tag-history-label">${escapeHtml(renderedLabel)}</span>` : ''}
                                    ${renderDispositionBadge(t)}
                                    ${confStr ? `<span class="tag-history-confidence">(${confStr})</span>` : ''}
                                    ${t.note ? `<div class="tag-history-meta">${escapeHtml(t.note)}</div>` : ''}
                                </div>
                                <div style="text-align: right;">
                                    <span class="tag-history-badge tag-history-badge-${t.type}">${t.type}</span>
                                    <div class="tag-history-meta">${dateStr} ${actorStr}</div>
                                </div>
                            </div>
                        `;
                    }).join('')}
                </div>
            `;
        }

        const currentLabel = manualTag && manualTag.label ? manualTag.label : '';
        const currentNote = manualTag ? (manualTag.note || '') : '';

        return `
            <div class="audio-event-card" id="audio-card-${eventId}">
                <div class="audio-card-header">
                    <div class="audio-card-meta">
                        <div class="audio-card-time">${timeStr}</div>
                        <div class="audio-card-source">
                            <strong>${escapeHtml(source)}</strong>
                            <span class="audio-badge ${badgeClass}">${badgeText}</span>
                            ${roomInfo ? `<span style="opacity: 0.8;">| ${escapeHtml(roomInfo)}</span>` : ''}
                        </div>
                        ${currentDisposition ? `<div class="audio-current-review">現在の扱い: <span class="audio-review-badge ${currentDisposition.className}">${currentDisposition.label}</span></div>` : ''}
                    </div>
                    <div class="audio-card-features">
                        <span class="feature-tag">長さ: <strong>${duration}</strong></span>
                        <span class="feature-tag">ピーク: <strong>${peakDb}</strong></span>
                        <span class="feature-tag">平均: <strong>${meanDb}</strong></span>
                        <span class="feature-tag">帯域: <strong>${band}</strong></span>
                        <span class="feature-tag">重心: <strong>${centroid}</strong></span>
                        ${isTransient ? `<span class="feature-tag" style="background-color: rgba(204,90,55,0.05); color: var(--claude-accent);">★ ${isTransient}</span>` : ''}
                        ${isPeriodic ? `<span class="feature-tag" style="background-color: rgba(3,105,161,0.05); color: #0369a1;">⟳ ${isPeriodic}</span>` : ''}
                    </div>
                </div>

                <div class="audio-playback-container">
                    <span style="font-size: 12px; font-weight: 600; color: var(--claude-text-sub);">録音再生:</span>
                    <audio controls src="${base}/api/audio-events/${eventId}/wav" preload="none"></audio>
                </div>

                <div class="audio-card-body">
                    <div class="audio-tag-section">
                        <form class="audio-label-form" onsubmit="saveAudioTag(event, '${eventId}')">
                            <div class="form-group" style="margin-bottom: 8px;">
                                <label class="form-label" style="margin-bottom: 4px;">音のラベル (手動登録)</label>
                                <input type="text" class="form-input" name="label" placeholder="例: キーボード音、咳払い、犬の鳴き声..." value="${escapeHtml(currentLabel)}">
                            </div>
                            <div class="form-group" style="margin-bottom: 12px;">
                                <label class="form-label" style="margin-bottom: 4px;">メモ (任意)</label>
                                <input type="text" class="form-input" name="note" placeholder="例: 実際に聞いて確認、かなり近かった" value="${escapeHtml(currentNote)}">
                            </div>
                            <div class="audio-review-actions">
                                <button type="button" class="btn btn-secondary btn-sm" onclick="quickReviewAction(this, '${eventId}', 'ignore')">無視</button>
                                <button type="button" class="btn btn-secondary btn-sm" onclick="quickReviewAction(this, '${eventId}', 'important')">重要</button>
                                <button type="button" class="btn btn-secondary btn-sm" onclick="quickReviewAction(this, '${eventId}', 'notify')">次から知らせる</button>
                                <button type="button" class="btn btn-secondary btn-sm" onclick="quickReviewAction(this, '${eventId}', 'silent_record')">黙って記録だけ</button>
                            </div>
                            <div style="display: flex; gap: 8px; justify-content: flex-end;">
                                <button type="submit" class="btn btn-primary btn-sm">ラベルを保存</button>
                            </div>
                        </form>
                    </div>
                    <div>
                        ${tagHistoryHtml || '<div style="font-size: 12px; color: var(--claude-text-sub); text-align: center; padding-top: 24px;">推論候補や登録履歴はまだありません</div>'}
                    </div>
                </div>
            </div>
        `;
    }).join('');
}

function escapeHtml(str) {
    return String(str ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

async function postAudioTag(eventId, payload) {
    const response = await fetch(`${base}/api/audio-events/${eventId}/tags`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify(payload)
    });
    if (!response.ok) {
        const text = await response.text();
        throw new Error(text || '保存に失敗しました');
    }
    return response.json();
}

async function saveAudioTag(e, eventId) {
    if (e) e.preventDefault();

    let form;
    let label = '';
    let note = '';

    if (e) {
        form = e.target;
        label = form.elements.label.value.trim();
        note = form.elements.note.value.trim();
    } else {
        return;
    }

    if (!label) {
        alert('ラベルを入力してください');
        return;
    }

    const saveBtn = form.querySelector('button[type="submit"]');
    if (saveBtn) saveBtn.disabled = true;

    try {
        await postAudioTag(eventId, {
            type: 'manual',
            label,
            confidence: 0.95,
            note,
            actor: 'user'
        });
        await fetchAudioEvents();
    } catch (err) {
        console.error('[Audio] Failed to save tag:', err);
        alert(`エラーが発生しました: ${err.message}`);
    } finally {
        if (saveBtn) saveBtn.disabled = false;
    }
}

async function quickReviewAction(btn, eventId, disposition) {
    const labels = {
        ignore: '無視',
        important: '重要',
        notify: '次から知らせる',
        silent_record: '黙って記録だけ'
    };
    const notes = {
        ignore: 'UIから無視として登録',
        important: 'UIから重要イベントとして登録',
        notify: 'UIから今後の通知候補として登録',
        silent_record: 'UIから黙って記録だけに設定'
    };
    btn.disabled = true;
    try {
        await postAudioTag(eventId, {
            type: 'manual',
            disposition,
            label: labels[disposition],
            confidence: 0.95,
            note: notes[disposition],
            actor: 'user'
        });
        await fetchAudioEvents();
    } catch (err) {
        console.error('[Audio] Failed to save review action:', err);
        alert(`エラー: ${err.message}`);
    } finally {
        btn.disabled = false;
    }
}
