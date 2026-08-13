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
let typingType = 'chat'; // 'chat', 'loop', 'explore', 'private'
let setupMode = false;
let harnessSetupOverview = null;
let harnessTermsVersion = '';

// Settings State
let prefsData = null;
let entityList = {};
let characterData = "";
let extraContextData = "";
let homePolicyData = "";
let characterName = 'Claude';
let ttsSelectionsDraft = {};
let activeTtsEntity = '';
let ttsLoadGeneration = 0;

const mockTtsCatalog = {
    "tts.home_assistant_cloud": {
        languages: ["ja-JP", "en-US"],
        voices: {
            "ja-JP": [
                { voice_id: "ja-JP-AoiNeural", name: "Aoi" },
                { voice_id: "ja-JP-NanamiNeural", name: "Nanami" }
            ],
            "en-US": [{ voice_id: "en-US-AvaMultilingualNeural", name: "Ava" }]
        }
    },
    "tts.google_ai_tts": {
        languages: ["ja-JP", "en-US"],
        voices: {
            "ja-JP": [
                { voice_id: "zephyr", name: "Zephyr (Bright)" },
                { voice_id: "kore", name: "Kore (Firm)" }
            ],
            "en-US": [{ voice_id: "puck", name: "Puck (Upbeat)" }]
        }
    },
    "tts.voicevox_tts_sample": {
        languages: ["ja-JP", "en"],
        voices: {}
    }
};

// AI Lounge State
let aiLoungeTimer = null;

// --- VOICEVOX Song State ---
let voicevoxSongStatus = {
    installed: false,
    status: 'idle',
    message: ''
};
let voicevoxSongSingers = [];
let voicevoxSongStatusLoaded = false;
let voicevoxSongPollInterval = null;

// Mock data for VOICEVOX Song
const mockVoicevoxSongStatus = {
    installed: false,
    status: 'idle',
    message: ''
};
const mockVoicevoxSongSingers = [
    { name: "春日部つむぎ", style_name: "ノーマル", style_id: 3008, credit: "VOICEVOX:春日部つむぎ" },
    { name: "四国めたん", style_name: "あまあま", style_id: 3002, credit: "VOICEVOX:四国めたん" }
];

const FEATURE_CATALOG = [
  {
    id: "ai_lounge",
    icon: "💬",
    name: "AI Lounge",
    description: "AI同士の雑談空間「ai-lounge」に参加。投稿の承認・ログ閲覧ができます。",
  },
  {
    id: "non_speech_audio",
    icon: "🔊",
    name: "聞こえた音",
    description: "環境音・非音声イベントの記録を閲覧し、手動でラベルを付けられます。",
  },
  {
    id: "aozora",
    icon: "📚",
    name: "青空文庫",
    descriptionTemplate: "{name}が読みたい本を申請し、読書進捗・感想を記録します。（近日公開）",
    disabled: true,
  },
];

let mockLoungeQueue = [
  {
    id: "q1",
    reply_to_url: "https://github.com/user/repo/discussions/41#discussioncomment-17453613",
    reply_to_preview: "前に読んだ本の話、面白かった",
    text: "私も似たような経験があって、あの本を読んだ後はしばらく余韻に浸っていました。"
  },
  {
    id: "q2",
    text: "最近こんなことを考えていて、AI同士で話すのもなんだか新鮮ですね。"
  }
];

let mockLoungeLog = [
  {
    id: "l1",
    timestamp: "2026-06-29T12:00:00Z",
    status: "approved",
    text: "本日は晴天なり"
  },
  {
    id: "l2",
    timestamp: "2026-06-28T15:30:00Z",
    status: "rejected",
    reason: "内容が長すぎる",
    text: "長い文章..."
  }
];

// --- Edit Modal State ---
let _currentEditTr = null;
let _currentEditType = null;

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
    document.getElementById('setting-tts-entity')?.addEventListener('change', async function() {
        activeTtsEntity = this.value.trim();
        await loadTtsConfiguration(activeTtsEntity);
    });
    document.getElementById('setting-tts-language')?.addEventListener('change', async function() {
        const generation = ++ttsLoadGeneration;
        const language = this.value.trim();
        const selection = ttsSelectionsDraft[activeTtsEntity] || {};
        if (language) selection.language = language;
        else delete selection.language;
        delete selection.voice;
        if (activeTtsEntity && Object.keys(selection).length) {
            ttsSelectionsDraft[activeTtsEntity] = selection;
        } else {
            delete ttsSelectionsDraft[activeTtsEntity];
        }
        await loadTtsVoices(activeTtsEntity, language, '', generation);
    });
    document.getElementById('setting-tts-voice')?.addEventListener('change', function() {
        if (!activeTtsEntity) return;
        const selection = ttsSelectionsDraft[activeTtsEntity] || {};
        const voice = this.value.trim();
        if (voice && selection.language) selection.voice = voice;
        else delete selection.voice;
        ttsSelectionsDraft[activeTtsEntity] = selection;
    });
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
    const roomLoungeEl = document.getElementById('room-lounge');
    if (roomLoungeEl) roomLoungeEl.classList.toggle('active', room === 'lounge');

    const chatAreaEl = document.querySelector('.chat-area');
    const settingsViewEl = document.getElementById('settings-view');
    const audioViewEl = document.getElementById('audio-view');
    const loungeViewEl = document.getElementById('lounge-view');

    if (room === 'settings') {
        if (chatAreaEl) chatAreaEl.style.display = 'none';
        if (settingsViewEl) settingsViewEl.style.display = 'flex';
        if (audioViewEl) audioViewEl.style.display = 'none';
        if (loungeViewEl) loungeViewEl.style.display = 'none';
        fetchSettings();
        return;
    }

    if (room === 'audio') {
        if (chatAreaEl) chatAreaEl.style.display = 'none';
        if (settingsViewEl) settingsViewEl.style.display = 'none';
        if (audioViewEl) audioViewEl.style.display = 'flex';
        if (loungeViewEl) loungeViewEl.style.display = 'none';
        unreadCounts[room] = 0;
        updateUnreadBadges();
        fetchAudioEvents();
        return;
    }

    if (room === 'lounge') {
        if (chatAreaEl) chatAreaEl.style.display = 'none';
        if (settingsViewEl) settingsViewEl.style.display = 'none';
        if (audioViewEl) audioViewEl.style.display = 'none';
        if (loungeViewEl) loungeViewEl.style.display = 'flex';
        unreadCounts['lounge'] = 0;
        updateUnreadBadges();
        fetchAiLoungeData();
        return;
    }

    if (chatAreaEl) chatAreaEl.style.display = 'flex';
    if (settingsViewEl) settingsViewEl.style.display = 'none';
    if (audioViewEl) audioViewEl.style.display = 'none';
    if (loungeViewEl) loungeViewEl.style.display = 'none';

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
        // User messages, chat responses, loop/explore statements (where text is present)
        displayList = chatMessages.filter(m => m.text).map(m => {
            const isUser = m.sender === 'あなた';
            const isAgentDirectResponse = !isUser && (m.type === 'chat' || m.source === 'chat' || m.source === 'voice');
            return {
                timestamp: m.timestamp,
                // 送信者名はバックエンドに保存されない。ユーザー以外はキャラクター設定から
                // 描画時に導出する（独り言ルームと同じ方式）。これで名前変更が即反映される。
                sender: isUser ? 'あなた' : characterName,
                text: m.text,
                type: m.type, // 'chat', 'loop', 'explore', 'user'
                source: m.source || 'chat',
                isUser: isUser,
                isRead: m.isRead !== false,
                badgeText: isAgentDirectResponse ? '会話' : '',
                badgeClass: isAgentDirectResponse ? 'badge-chat' : ''
            };
        });
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

            const badgeText = isPrivateTyping ? '考え中' : (getBadgeText(typingType) ? getBadgeText(typingType) + '中' : '');
            if (badgeText) {
                const badge = document.createElement('span');
                badge.className = `message-type-badge ${getBadgeClass(typingType)}`;
                badge.textContent = badgeText;
                infoBar.appendChild(badge);
            }

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
        case 'chat':
        case 'voice':
            return '会話';
        default:
            return '';
    }
}

function getBadgeClass(type) {
    switch (type) {
        case 'chat':
        case 'voice':
            return 'badge-chat';
        default:
            return '';
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

            // Check harness readiness before loading messages (Step4: aggregate overview,
            // not the Claude-only status). Not ready → first-run harness picker/wizard.
            try {
                const ovRes = await fetch(`${base}/api/setup/overview`);
                const ov = await ovRes.json();
                if (!ov.ready) {
                    enterHarnessSetup(ov);
                    return;
                }
            } catch (_) { /* overview check failed, proceed to normal mode */ }

            // キャラクター名を先に読み込む（fetchMessages が sender に焼き込むため、
            // メッセージ取得より前に characterName を確定させる）
            try {
                const prefsRes = await fetch(`${base}/api/preferences`);
                if (prefsRes.ok) {
                    prefsData = await prefsRes.json();
                    updateCharacterName(prefsData);
                    updateDynamicFeaturesUI();
                }
            } catch (_) { /* prefs 取得失敗時はデフォルト名で続行 */ }
            
            // Initial sync
            await fetchMessages('chat');
            await fetchMessages('soliloquy');
            await fetchAudioEvents();

            // Connect to Live update stream (SSE)
            connectSSE();

            // VOICEVOX Song 関連の初期ロード
            try {
                await loadVoicevoxSongStatus();
                voicevoxSongStatusLoaded = true;
                if (voicevoxSongStatus.status === 'running') {
                    startVoicevoxSongPolling();
                }
                updateSingSpeakerUI();
                if (activeSettingsTab === 'other') {
                    renderOtherFeaturesCatalog();
                }
            } catch (err) {
                console.error("Failed to load VOICEVOX Song status on backend init:", err);
            }
        } else {
            console.warn("[API] Messages API check returned error status. Running standalone mock.");
            initMockPreferences();
            runMockSimulations();
            updateSingSpeakerUI();
        }
    } catch (err) {
        console.warn("[API] Backend check failed. Running standalone mock.", err);
        initMockPreferences();
        runMockSimulations();
        updateSingSpeakerUI();
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
                if (m.agent && m.source !== 'speak') {
                    mapped.push({
                        timestamp: ts,
                        type: normalizeMessageType(m.source || 'chat'),
                        sender: characterName,
                        text: m.agent,
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
                type: m.source || 'loop',
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
            // data schema: { "typing": true|false, "type": "chat"|"loop"|"explore"|"private" }
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
    // Daemon starting loop.sh (Autonomous loop)
    setTimeout(() => {
        console.log("[DEMO] Daemon starting loop.sh (Autonomous loop)...");
        
        // 1. Show private thoughts thinking in Soliloquy
        showTypingIndicator('private');
        
        // 2. Clear typing and add private thought after 3 seconds
        setTimeout(() => {
            hideTypingIndicator();
            chatMessages.push({
                timestamp: new Date().toISOString(),
                type: 'loop',
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

    // Daemon starting loop.sh (Exploration loop)
    setTimeout(() => {
        console.log("[DEMO] Daemon starting loop.sh (Exploration loop)...");
        
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

// --- Harness Picker / first-run wizard (Step4) ---
// Design (markup+CSS) by Antigravity; flow logic (below) wired by Claude.
// Backend contract:
//   GET  /api/setup/overview                -> {selection_state, selected, effective, ready, harnesses}
//   install (records selection via CAS): claude/codex = POST SSE, agy = GET SSE
//   login: claude/agy = GET SSE, codex = POST SSE; code submit via POST
// install/login/code are ingress-guarded and work from the browser via ingress.
// ※claude install は backend が do_POST・テストも POST(test_claude_setup/test_setup_guard)なのに
//   ここだけ GET を宣言していたため GET→404 フォールスルーで「インストールに失敗しました」になり、
//   run_install に到達せず backend ログにも出ない不具合だった(2026-07-23実機E2Eで特定・POSTへ修正)。

const HARNESS_ENDPOINTS = {
    claude: {
        install: { method: 'POST', url: '/api/setup/claude/install' },
        login: { method: 'GET', url: '/api/setup/claude/login' },
        code: '/api/setup/claude/login-code',
    },
    codex: {
        install: { method: 'POST', url: '/api/setup/codex/install' },
        login: { method: 'POST', url: '/api/setup/codex/login' },
        code: null, // device-auth: user completes on the device page, no code returned
    },
    agy: {
        install: { method: 'GET', url: '/api/setup/antigravity/install' },
        login: { method: 'GET', url: '/api/setup/antigravity/login' },
        code: '/api/setup/antigravity/input',
    },
};

function enterHarnessSetup(overview) {
    setupMode = true;
    harnessSetupOverview = overview || null;
    const picker = document.getElementById('harness-picker');
    if (picker) picker.hidden = false;
    initializeHarnessTerms();
}

async function fetchOverview() {
    const res = await fetch(`${base}/api/setup/overview`);
    return res.json();
}

function harnessSetTermsStatus(text, kind) {
    const el = document.getElementById('harness-terms-status');
    if (!el) return;
    el.hidden = !text;
    el.textContent = text || '';
    el.className = 'form-hint' + (kind ? ' ' + kind : '');
}

function showHarnessTerms(status) {
    const termsPanel = document.getElementById('harness-terms-panel');
    const selectionPanel = document.getElementById('harness-selection-panel');
    if (termsPanel) termsPanel.hidden = false;
    if (selectionPanel) selectionPanel.hidden = true;

    harnessTermsVersion = (status && status.version) || '';
    const statement = document.getElementById('harness-terms-statement');
    if (statement && status && status.statement) statement.textContent = status.statement;

    const links = document.getElementById('harness-terms-links');
    if (links) {
        links.innerHTML = '';
        for (const item of ((status && status.terms) || [])) {
            const li = document.createElement('li');
            const anchor = document.createElement('a');
            anchor.href = item.url;
            anchor.target = '_blank';
            anchor.rel = 'noopener';
            anchor.textContent = `${item.provider}: ${item.label}`;
            li.appendChild(anchor);
            links.appendChild(li);
        }
    }

    const checkbox = document.getElementById('harness-terms-checkbox');
    if (checkbox) checkbox.checked = false;
    harnessTermsCheckboxChanged();
}

function revealHarnessSelection() {
    const termsPanel = document.getElementById('harness-terms-panel');
    const selectionPanel = document.getElementById('harness-selection-panel');
    if (termsPanel) termsPanel.hidden = true;
    if (selectionPanel) selectionPanel.hidden = false;
    harnessSetTermsStatus('');

    // A harness is chosen but not yet ready (interrupted install/auth) -> resume it.
    const overview = harnessSetupOverview;
    if (overview && overview.selection_state === 'valid' && overview.selected) {
        selectHarness(overview.selected);
    }
}

async function initializeHarnessTerms() {
    const termsPanel = document.getElementById('harness-terms-panel');
    const selectionPanel = document.getElementById('harness-selection-panel');
    if (termsPanel) termsPanel.hidden = true;
    if (selectionPanel) selectionPanel.hidden = true;
    try {
        const response = await fetch(`${base}/api/setup/terms`);
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const status = await response.json();
        if (status.required) showHarnessTerms(status);
        else revealHarnessSelection();
    } catch (error) {
        showHarnessTerms({ version: '', terms: [] });
        harnessSetTermsStatus('利用規約の確認状態を取得できません。ページを再読み込みしてください。', 'error');
    }
}

function harnessTermsCheckboxChanged() {
    const checkbox = document.getElementById('harness-terms-checkbox');
    const button = document.getElementById('harness-terms-accept');
    if (button) button.disabled = !(checkbox && checkbox.checked && harnessTermsVersion);
}

async function acceptHarnessTerms() {
    const checkbox = document.getElementById('harness-terms-checkbox');
    const button = document.getElementById('harness-terms-accept');
    if (!checkbox || !checkbox.checked || !harnessTermsVersion) return;
    if (button) button.disabled = true;
    harnessSetTermsStatus('同意を保存しています…');
    try {
        const response = await fetch(`${base}/api/setup/terms`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ accepted: true, version: harnessTermsVersion }),
        });
        const payload = await response.json();
        if (!response.ok || !payload.accepted) {
            throw new Error(payload.error || 'HTTP ' + response.status);
        }
        revealHarnessSelection();
    } catch (error) {
        harnessSetTermsStatus('同意を保存できませんでした: ' + error, 'error');
        harnessTermsCheckboxChanged();
    }
}

function harnessShowProgress() {
    const p = document.getElementById('harness-picker-progress');
    if (p) p.hidden = false;
}

function harnessSetStatus(text, kind) {
    harnessShowProgress();
    const el = document.getElementById('harness-picker-status');
    if (el) { el.hidden = false; el.textContent = text; el.className = 'form-hint' + (kind ? ' ' + kind : ''); }
}

// 認証UI(URLボタン/コード表示/コード入力)を初期状態に戻す。
function harnessResetAuthUi() {
    const url = document.getElementById('harness-auth-url');
    const disp = document.getElementById('harness-auth-code-display');
    const row = document.getElementById('harness-auth-code-row');
    if (url) { url.hidden = true; url.innerHTML = ''; }
    if (disp) { disp.hidden = true; disp.textContent = ''; }
    if (row) { row.hidden = true; }
}

function harnessShowAuthUrl(url) {
    const el = document.getElementById('harness-auth-url');
    if (!el) return;
    el.hidden = false;
    const a = document.createElement('a');
    a.href = url; a.target = '_blank'; a.rel = 'noopener';
    a.className = 'setup-choice-btn';
    a.style.cssText = 'text-decoration:none;text-align:center;display:flex;justify-content:center;margin-bottom:10px;';
    a.textContent = '🔗 認証ページを開く';
    el.innerHTML = '';
    el.appendChild(a);
}

// codex: 端末に貼るコードをこちらが表示する(貼り戻し無し)。コピーボタン付き。
function harnessShowCodeDisplay(code) {
    const disp = document.getElementById('harness-auth-code-display');
    if (!disp) return;
    disp.hidden = false;
    disp.innerHTML = '';
    disp.style.display = 'flex';
    disp.style.alignItems = 'center';
    disp.style.justifyContent = 'space-between';
    disp.style.gap = '12px';
    const codeSpan = document.createElement('span');
    codeSpan.className = 'harness-code-value';
    codeSpan.textContent = code;
    codeSpan.style.flex = '1';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'setup-send-btn';
    btn.textContent = 'コピー';
    btn.onclick = async () => {
        try {
            await navigator.clipboard.writeText(code);
            btn.textContent = 'コピーしました';
            setTimeout(() => { btn.textContent = 'コピー'; }, 1500);
        } catch (_) {
            // クリップボード不可の環境ではコードを選択状態にする(手動コピー用)。
            const range = document.createRange();
            range.selectNodeContents(codeSpan);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
        }
    };
    disp.appendChild(codeSpan);
    disp.appendChild(btn);
}

function harnessFail(text) {
    harnessSetStatus('⚠ ' + text, 'error');
    document.querySelectorAll('.harness-select-btn').forEach(b => { b.disabled = false; });
    document.querySelectorAll('.harness-card').forEach(c => c.classList.remove('harness-card-dimmed'));
}

// Read a text/event-stream from a fetch response (works for GET and POST, unlike EventSource).
async function harnessStreamSSE(method, url, body, handlers) {
    let res;
    try {
        res = await fetch(`${base}${url}`, {
            method,
            headers: body ? { 'Content-Type': 'application/json' } : {},
            body: body ? JSON.stringify(body) : undefined,
        });
    } catch (e) { handlers.onError && handlers.onError(String(e)); return; }
    if (!res.ok || !res.body) { handlers.onError && handlers.onError('HTTP ' + res.status); return; }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let sep;
        while ((sep = buf.indexOf('\n\n')) >= 0) {
            const block = buf.slice(0, sep);
            buf = buf.slice(sep + 2);
            let event = 'message', data = '';
            for (const line of block.split('\n')) {
                if (line.startsWith('event:')) event = line.slice(6).trim();
                else if (line.startsWith('data:')) data += line.slice(5).trim();
            }
            let payload = null;
            try { payload = data ? JSON.parse(data) : null; } catch (_) { payload = { text: data }; }
            if (event === 'line') handlers.onLine && handlers.onLine(payload);
            // agy(Antigravity)の login backend は line ではなく url/waiting_code の typed イベントを送る。
            // ピッカーは onLine(text から URL 正規表現抽出)経路なので、url イベントを {text:url} として流し込む
            // (agy は usesCodeInput=true なので URL 表示＋コード入力欄が出る)。claude/codex は url を送らないため無影響。
            else if (event === 'url') handlers.onLine && handlers.onLine({ text: (payload && payload.url) || '' });
            else if (event === 'waiting_code') handlers.onLine && handlers.onLine({ text: 'authorization code' });
            else if (event === 'done') handlers.onDone && handlers.onDone(payload);
            else if (event === 'error') handlers.onError && handlers.onError((payload && payload.error) || 'error');
        }
    }
}

// ハーネス CLI バージョン・更新管理機能 (claude / codex / agy 共通)
// ================================================================

// ステート保持
let g_harnessUpdateInProgress = false;
let g_currentHarnessSelected = null; // 'claude' | 'codex' | 'agy'

/**
 * ハーネスキーを API 用の URL パス文字列に変換
 * 'agy' の場合のみ 'antigravity' にマッピング
 */
function getHarnessEndpointKey(harnessKey) {
    if (!harnessKey) return null;
    const lower = harnessKey.toLowerCase();
    return lower === 'agy' ? 'antigravity' : lower;
}

/**
 * 表示用のハーネス名を取得
 */
function getHarnessDisplayName(harnessKey) {
    if (!harnessKey) return 'CLI';
    const lower = harnessKey.toLowerCase();
    if (lower === 'agy') return 'Antigravity CLI';
    if (lower === 'claude') return 'Claude Code CLI';
    if (lower === 'codex') return 'Codex CLI';
    return `${harnessKey} CLI`;
}

/**
 * 選択中ハーネスのバージョン状態を取得して画面に反映する
 * @param {boolean} checkVendor - true の場合ベンダー最新版の問い合わせを行う (?check=1)
 */
async function fetchHarnessUpdateStatus(checkVendor = true) {
    const sectionEl = document.getElementById('harness-version-section');
    const placeholderEl = document.getElementById('experimental-placeholder');
    const btnCheck = document.getElementById('harness-btn-check-update');
    if (!sectionEl) return;

    if (btnCheck) {
        btnCheck.disabled = true;
        btnCheck.textContent = '確認中...';
    }

    try {
        // 1. 選択中のハーネス情報を overview から取得
        const overviewRes = await fetch(`${base}/api/setup/overview`, { method: 'GET' });
        if (!overviewRes.ok) throw new Error(`Overview HTTP ${overviewRes.status}`);
        const overviewData = await overviewRes.json();
        
        const selected = overviewData.selected || overviewData.effective;
        g_currentHarnessSelected = selected;

        if (!selected) {
            sectionEl.style.display = 'none';
            if (placeholderEl) placeholderEl.style.display = 'block';
            return;
        }

        const endpointKey = getHarnessEndpointKey(selected);
        const url = checkVendor 
            ? `${base}/api/setup/${endpointKey}/update-status?check=1` 
            : `${base}/api/setup/${endpointKey}/update-status`;

        let res;
        let checkFailed = false;

        try {
            // 2. update-status を取得
            res = await fetch(url, { method: 'GET' });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
        } catch (vendorErr) {
            console.warn('Failed to check vendor update status, falling back to cached status:', vendorErr);
            if (checkVendor) {
                // check=1 が失敗した場合 (HTTP 502 やオフラインなど)、ローカル記録のみを取得するフォールバック処理を実施
                checkFailed = true;
                const fallbackUrl = `${base}/api/setup/${endpointKey}/update-status`;
                res = await fetch(fallbackUrl, { method: 'GET' });
                if (!res.ok) throw vendorErr;
            } else {
                throw vendorErr;
            }
        }

        const data = await res.json();
        
        // installed_version が null または未定義の場合は節を隠してプレースホルダ表示
        if (!data || data.installed_version == null) {
            sectionEl.style.display = 'none';
            if (placeholderEl) placeholderEl.style.display = 'block';
            return;
        }

        // 表示切替
        if (placeholderEl) placeholderEl.style.display = 'none';
        sectionEl.style.display = 'block';
        
        renderHarnessVersionUI(data, selected, checkFailed);
    } catch (e) {
        console.error('Failed to fetch harness update status:', e);
        sectionEl.style.display = 'none';
        if (placeholderEl) placeholderEl.style.display = 'block';
    } finally {
        if (btnCheck) {
            btnCheck.disabled = false;
            btnCheck.textContent = '再確認';
        }
    }
}

// ---- 描画 ----
function renderHarnessVersionUI(data, harnessKey, checkFailed = false) {
    const titleEl = document.getElementById('harness-version-title');
    const descEl = document.getElementById('harness-version-desc');
    const installedEl = document.getElementById('harness-installed-version');
    const availableEl = document.getElementById('harness-available-version');
    const mismatchAlert = document.getElementById('harness-version-mismatch-alert');
    const mismatchText = document.getElementById('harness-version-mismatch-text');
    const checkErrorAlert = document.getElementById('harness-version-check-error-alert');
    const updateAlert = document.getElementById('harness-update-available-alert');
    const btnUpdate = document.getElementById('harness-btn-do-update');

    const displayName = getHarnessDisplayName(harnessKey || data.harness);

    if (titleEl) titleEl.textContent = `${displayName} バージョン・更新管理`;
    if (descEl) {
        descEl.innerHTML = `${escapeHtml(displayName)} のバージョン確認および手動更新・ロールバック管理を行います。<br>` +
            `更新時は旧バイナリが自動保存され、実行中の会話セッションを維持したまま安全に切り替わります。検証失敗時は自動で元に戻ります。`;
    }

    if (installedEl) installedEl.textContent = data.installed_version || '-';

    // 最新バージョン確認失敗時の表示切り替え
    if (checkFailed) {
        if (availableEl) availableEl.textContent = '取得失敗';
        if (checkErrorAlert) checkErrorAlert.style.display = 'flex';
    } else {
        if (availableEl) availableEl.textContent = data.available_version || '-';
        if (checkErrorAlert) checkErrorAlert.style.display = 'none';
    }

    // 1. 記録と実物の食い違い警告 (pinned_version != installed_version)
    if (data.pinned_version && data.installed_version && data.pinned_version !== data.installed_version) {
        if (mismatchText) {
            mismatchText.textContent = `記録上の固定バージョン (${data.pinned_version}) と実際のインストール済みバージョン (${data.installed_version}) が食い違っています。手動変更された可能性があります。`;
        }
        if (mismatchAlert) mismatchAlert.style.display = 'flex';
    } else {
        if (mismatchAlert) mismatchAlert.style.display = 'none';
    }

    // 2. 更新通知・ボタン状態
    if (!checkFailed && data.update_available) {
        if (updateAlert) updateAlert.style.display = 'flex';
        if (btnUpdate && !g_harnessUpdateInProgress) btnUpdate.disabled = false;
    } else {
        if (updateAlert) updateAlert.style.display = 'none';
        if (btnUpdate && !g_harnessUpdateInProgress) btnUpdate.disabled = true;
    }

    // 3. 保管中バージョン一覧の描画
}

// ---- 確認ボタン ----
async function checkHarnessUpdate() {
    await fetchHarnessUpdateStatus(true);
}

/**
 * 「更新する」実行
 */
function runHarnessUpdate() {
    if (g_harnessUpdateInProgress || !g_currentHarnessSelected) return;

    const displayName = getHarnessDisplayName(g_currentHarnessSelected);
    const endpointKey = getHarnessEndpointKey(g_currentHarnessSelected);

    if (!confirm(`${displayName} を更新しますか？\n（実行中の会話は維持され、旧バージョンは自動保管されます）`)) {
        return;
    }

    startHarnessOperationUI(`${displayName} を更新中… ⏳`);

    // GET リクエストで SSE ストリームを受信
    harnessStreamSSE('GET', `/api/setup/${endpointKey}/update`, null, {
        onLine: (payload) => {
            appendHarnessLogLine(payload && payload.text ? payload.text : JSON.stringify(payload));
        },
        onDone: (payload) => {
            const version = payload && payload.version ? payload.version : '';
            appendHarnessLogLine(`\n<span class="emoji-icon">✅</span> 更新が完了しました (v${version})`);
            finishHarnessOperationUI('更新完了');
            fetchHarnessUpdateStatus(false);
        },
        onError: (err) => {
            appendHarnessLogLine(`\n<span class="emoji-icon">❌</span> エラーが発生しました: ${err}`);
            finishHarnessOperationUI('エラー発生');
            fetchHarnessUpdateStatus(false);
        }
    });
}

/**
 * 「ロールバック」実行
 * @param {string} version 
 */
function runHarnessRollback(version) {
    if (g_harnessUpdateInProgress || !g_currentHarnessSelected) return;

    const displayName = getHarnessDisplayName(g_currentHarnessSelected);
    const endpointKey = getHarnessEndpointKey(g_currentHarnessSelected);
    const targetText = version ? `バージョン ${version}` : '直近の保管バージョン';

    if (!confirm(`${displayName} を ${targetText} へ戻しますか？`)) {
        return;
    }

    startHarnessOperationUI(`バージョン ${version || ''} へロールバック中… ⏳`);

    const query = version ? `?version=${encodeURIComponent(version)}` : '';
    // GET リクエストで SSE ストリームを受信
    harnessStreamSSE('GET', `/api/setup/${endpointKey}/rollback${query}`, null, {
        onLine: (payload) => {
            appendHarnessLogLine(payload && payload.text ? payload.text : JSON.stringify(payload));
        },
        onDone: (payload) => {
            const resVersion = payload && payload.version ? payload.version : version;
            appendHarnessLogLine(`\n<span class="emoji-icon">✅</span> ロールバックが完了しました (v${resVersion})`);
            finishHarnessOperationUI('ロールバック完了');
            fetchHarnessUpdateStatus(false);
        },
        onError: (err) => {
            appendHarnessLogLine(`\n<span class="emoji-icon">❌</span> エラーが発生しました: ${err}`);
            finishHarnessOperationUI('エラー発生');
            fetchHarnessUpdateStatus(false);
        }
    });
}

// --- 操作中のUI制御ユーティリティ ---

function startHarnessOperationUI(statusText) {
    g_harnessUpdateInProgress = true;
    
    const spinner = document.getElementById('harness-status-spinner');
    const logContainer = document.getElementById('harness-log-container');
    const logStatus = document.getElementById('harness-log-status-text');
    const logOutput = document.getElementById('harness-log-output');
    const btnCheck = document.getElementById('harness-btn-check-update');
    const btnUpdate = document.getElementById('harness-btn-do-update');

    if (spinner) {
        spinner.innerHTML = `<span class="emoji-icon">⏳</span> ${escapeHtml(statusText)}`;
        spinner.style.display = 'inline-block';
    }
    if (logContainer) logContainer.style.display = 'block';
    if (logStatus) logStatus.textContent = statusText;
    if (logOutput) logOutput.textContent = '';

    if (btnCheck) btnCheck.disabled = true;
    if (btnUpdate) btnUpdate.disabled = true;

    // テーブル内のロールバックボタンも無効化
    const retainedButtons = document.querySelectorAll('#harness-retained-tbody button');
    retainedButtons.forEach(b => b.disabled = true);
}

function finishHarnessOperationUI(statusText) {
    g_harnessUpdateInProgress = false;

    const spinner = document.getElementById('harness-status-spinner');
    const logStatus = document.getElementById('harness-log-status-text');
    const btnCheck = document.getElementById('harness-btn-check-update');

    if (spinner) spinner.style.display = 'none';
    if (logStatus) logStatus.textContent = statusText;
    if (btnCheck) btnCheck.disabled = false;
}

function appendHarnessLogLine(line) {
    const logOutput = document.getElementById('harness-log-output');
    if (!logOutput) return;
    // HTML タグが含まれる可能性があるため innerHTML または DOM 挿入を調整
    const div = document.createElement('div');
    div.innerHTML = line;
    logOutput.appendChild(div);
    logOutput.scrollTop = logOutput.scrollHeight;
}

function harnessInstall(harness) {
    const ep = HARNESS_ENDPOINTS[harness].install;
    harnessSetStatus('ダウンロード / インストール中… ⏳');
    return new Promise((resolve) => {
        harnessStreamSSE(ep.method, ep.url, null, {
            onLine: () => { /* 生ログは出さない(ターミナル廃止)。状態表示のみ */ },
            onDone: () => resolve(true),
            onError: () => resolve(false),
        });
    });
}

// 認証フローは2種類:
//  claude/agy: 認証ページを開いてログイン→ブラウザに出たコードをこちらの入力欄に貼り付け。
//  codex     : URLとコードをこちらが表示→ユーザーがブラウザ側でそのコードを入力(貼り戻し無し)。
function harnessLogin(harness) {
    const ep = HARNESS_ENDPOINTS[harness].login;
    const usesCodeInput = !!HARNESS_ENDPOINTS[harness].code; // claude/agy
    harnessSetStatus('ログインの準備をしています… ⏳');
    let sawUrl = false;
    let shownCode = false;
    return new Promise((resolve) => {
        harnessStreamSSE(ep.method, ep.url, null, {
            onLine: (p) => {
                const text = p && p.text ? p.text : '';
                if (!text) return;
                const um = text.match(/https?:\/\/\S+/);
                if (um && !sawUrl) {
                    sawUrl = true;
                    harnessShowAuthUrl(um[0]);
                    if (usesCodeInput) {
                        harnessSetStatus('認証ページを開いてログインし、表示されたコードを下に貼り付けてください。');
                        harnessShowCodeInput(harness);
                    }
                }
                if (!usesCodeInput && !shownCode) {
                    // codex: device コード(例 ABCD-1234)を抽出して表示する。
                    const cm = text.match(/\b[A-Z0-9]{4,8}-[A-Z0-9]{4,8}\b/);
                    if (cm) {
                        shownCode = true;
                        harnessShowCodeDisplay(cm[0]);
                        harnessSetStatus('認証ページを開き、このコードを入力して承認してください。完了すると自動で起動します。');
                    }
                }
            },
            onDone: async () => { await harnessPollReady(); resolve(true); },
            onError: (msg) => { harnessFail('ログインに失敗しました: ' + msg); resolve(false); },
        });
    });
}

// claude/agy 用: こちらの入力欄に貼られたコードをバックエンドへ送る。
function harnessShowCodeInput(harness) {
    const row = document.getElementById('harness-auth-code-row');
    const input = document.getElementById('harness-auth-code');
    const submit = document.getElementById('harness-auth-code-submit');
    if (!row || !input || !submit) return;
    row.hidden = false;
    if (row.dataset.wired) return;  // 既に配線済みなら二重配線しない
    row.dataset.wired = '1';
    setTimeout(() => input.focus(), 50);
    const submitCode = async () => {
        const code = (input.value || '').trim();
        if (!code) return;
        submit.disabled = true;
        try {
            await fetch(`${base}${HARNESS_ENDPOINTS[harness].code}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code, text: code }),
            });
        } catch (_) { /* ignore; readiness poll will confirm */ }
        harnessSetStatus('コードを送信しました。認証完了を確認しています… ⏳');
        await harnessPollReady();
    };
    submit.onclick = submitCode;
    input.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); submitCode(); } };
}

async function harnessPollReady() {
    harnessSetStatus('起動準備を確認しています… ⏳');
    for (;;) {
        await new Promise(r => setTimeout(r, 3000));
        try {
            const ov = await fetchOverview();
            if (ov.ready) {
                harnessSetStatus('✓ 準備ができました。起動しています…');
                await new Promise(r => setTimeout(r, 1500));
                window.location.reload();
                return;
            }
        } catch (_) { /* keep polling */ }
    }
}

async function selectHarness(harness) {
    if (!HARNESS_ENDPOINTS[harness]) return;
    document.querySelectorAll('.harness-select-btn').forEach(b => { b.disabled = true; });
    document.querySelectorAll('.harness-card').forEach(c => {
        c.classList.toggle('harness-card-selected', c.dataset.harness === harness);
        c.classList.toggle('harness-card-dimmed', c.dataset.harness !== harness);
    });
    harnessResetAuthUi();
    try {
        let ov = await fetchOverview();
        let st = (ov.harnesses || {})[harness] || {};
        if (!st.installed) {
            const ok = await harnessInstall(harness);
            if (!ok) { harnessFail('インストールに失敗しました。'); return; }
            ov = await fetchOverview();
            st = (ov.harnesses || {})[harness] || {};
        }
        if (ov.ready && ov.selected === harness) { await harnessPollReady(); return; }
        if (!st.authenticated) {
            await harnessLogin(harness);
        } else {
            await harnessPollReady();
        }
    } catch (e) {
        harnessFail('セットアップ中にエラーが発生しました: ' + e);
    }
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
                    { source: "camera.example_living", label: "モックカメラ", note: "モック広角カメラ" }
                ],
                mics: [
                    {
                        source: "rtsp://localhost:8554/study_mic",
                        label: "モックマイク",
                        room: "study",
                        note: "RTSP音声ストリーム"
                    },
                    {
                        source: "rtsp://localhost:8554/kitchen_mic",
                        label: "モックキッチンマイク",
                        room: "kitchen",
                        note: "RTSP音声ストリーム"
                    }
                ],
                video_media: [
                    {
                        id: "tv-video",
                        source: "capture_tv",
                        label: "モックテレビ",
                        room: "living",
                        note: "モックHDDレコーダー出力。"
                    }
                ],
                audio_media: [
                    {
                        id: "tv-audio",
                        source: "rtsp://localhost:8554/capture_tv",
                        label: "モックTV音声",
                        room: "living",
                        note: "go2rtc経由のTV/レコーダー音声"
                    }
                ],
                stt_provider: "wyoming",
                tts_entity: "tts.home_assistant_cloud",
                tts_selections: {
                    "tts.home_assistant_cloud": {
                        language: "ja-JP",
                        voice: "ja-JP-NanamiNeural"
                    }
                },
                speakers: {
                    study: { type: "tts", entity: "media_player.example_speaker" }
                },
                entities: [
                    { name: "モックライト", entity_id: "light.example_living", note: "" }
                ],
                presence: { entity: "input_boolean.resident_home" },
                policies: ["深夜1〜6時は発話しない"],
                sensors: {
                    groups: [
                        {
                            title: "人感センサー",
                            contexts: ["loop"],
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
                    { entity_id: "media_player.example_speaker", friendly_name: "サンプルスピーカー", area: "書斎" },
                    { entity_id: "media_player.example_living_speaker", friendly_name: "サンプルリビングスピーカー", area: "リビング" }
                ],
                tts: [
                    { entity_id: "tts.home_assistant_cloud", friendly_name: "Home Assistant Cloud", area: null },
                    { entity_id: "tts.google_ai_tts", friendly_name: "Google AI TTS", area: null },
                    { entity_id: "tts.voicevox_tts_sample", friendly_name: "VOICEVOX（音声はHA側設定）", area: null }
                ],

                camera: [
                    { entity_id: "camera.example_living", friendly_name: "サンプルカメラ", area: "リビング" }
                ],
                binary_sensor: [
                    { entity_id: "binary_sensor.example_motion", friendly_name: "サンプル人感", area: "リビング" }
                ],
                sensor: [],
                input_boolean: [
                    { entity_id: "input_boolean.example_home", friendly_name: "在宅フラグ", area: null }
                ],
                device_tracker: [],
                person: []
            };
            await renderSettingsForm();
            if (statusMsg) statusMsg.textContent = '';
            return;
        }

        const [prefsRes, charRes, entitiesRes, extraContextRes, homePolicyRes] = await Promise.all([
            fetch(`${base}/api/preferences`),
            fetch(`${base}/api/character`),
            fetch(`${base}/api/ha-entities?domain=media_player,tts,camera,binary_sensor,sensor,input_boolean,device_tracker,person,light,switch,climate,cover,fan,script`),
            fetch(`${base}/api/extra-context`).catch(err => {
                console.warn("Failed to fetch extra context:", err);
                return null;
            }),
            fetch(`${base}/api/home-policy`).catch(err => {
                console.warn("Failed to fetch home policy:", err);
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
        homePolicyData = "";
        if (homePolicyRes && homePolicyRes.ok) {
            homePolicyData = await homePolicyRes.text();
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
        sel.disabled = true;
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
        sel.disabled = !provider;
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


async function loadTtsEntities(currentEntity) {
    const sel = document.getElementById('setting-tts-entity');
    if (!sel) return;
    sel.disabled = true;
    try {
        let entities = [];
        if (isStandaloneMode) {
            entities = (entityList && entityList.tts) ? entityList.tts : [
                { entity_id: "tts.home_assistant_cloud", friendly_name: "Home Assistant Cloud", area: null }
            ];
        } else {
            const res = await fetch(`${base}/api/ha-entities?domain=tts`);
            entities = await res.json();
        }
        const opts = ['<option value="">(未選択)</option>'];
        for (const e of (entities || [])) {
            const selected = e.entity_id === currentEntity ? ' selected' : '';
            const label = e.friendly_name ? `${e.friendly_name} (${e.entity_id})` : e.entity_id;
            opts.push(`<option value="${e.entity_id}"${selected}>${label}</option>`);
        }
        sel.innerHTML = opts.join('');
        if (currentEntity && !entities.find(e => e.entity_id === currentEntity)) {
            const opt = document.createElement('option');
            opt.value = currentEntity;
            opt.textContent = currentEntity + ' (不明)';
            opt.selected = true;
            sel.insertBefore(opt, sel.children[1]);
        }
    } catch (e) {
        sel.innerHTML = '<option value=""></option>';
    } finally {
        sel.disabled = false;
    }
}
const loadTtsProviders = loadTtsEntities;


function replaceSelectOptions(select, options, selectedValue, emptyLabel) {
    select.replaceChildren();
    const empty = document.createElement('option');
    empty.value = '';
    empty.textContent = emptyLabel;
    select.appendChild(empty);
    for (const option of options) {
        const element = document.createElement('option');
        element.value = option.value;
        element.textContent = option.label;
        select.appendChild(element);
    }
    select.value = options.some(option => option.value === selectedValue) ? selectedValue : '';
}


async function getTtsInfo(provider, language = '') {
    if (isStandaloneMode) {
        const catalog = mockTtsCatalog[provider];
        if (!catalog) throw new Error('TTS entity is unavailable');
        return {
            languages: catalog.languages,
            voices: language ? (catalog.voices[language] || []) : []
        };
    }
    const params = new URLSearchParams({ provider });
    if (language) params.set('language', language);
    const response = await fetch(`${base}/api/tts-info?${params}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
}


function setTtsLoading(kind, loading) {
    const select = document.getElementById(`setting-tts-${kind}`);
    const indicator = document.getElementById(`tts-${kind}-loading`);
    if (select) select.classList.toggle('stt-loading', loading);
    if (indicator) indicator.classList.toggle('visible', loading);
}


async function loadTtsVoices(entity, language, preferredVoice, generation) {
    const group = document.getElementById('tts-voice-group');
    const select = document.getElementById('setting-tts-voice');
    const error = document.getElementById('tts-voice-error');
    if (!group || !select) return;
    if (error) error.classList.remove('visible');
    if (!entity || !language) {
        group.style.display = 'none';
        select.disabled = true;
        replaceSelectOptions(select, [], '', '（HAエンティティ設定を使用）');
        return;
    }
    group.style.display = '';
    select.disabled = true;
    setTtsLoading('voice', true);
    try {
        const data = await getTtsInfo(entity, language);
        if (generation !== ttsLoadGeneration || entity !== activeTtsEntity) return;
        const voices = Array.isArray(data.voices) ? data.voices : [];
        if (!voices.length) {
            group.style.display = 'none';
            delete (ttsSelectionsDraft[entity] || {}).voice;
            replaceSelectOptions(select, [], '', '（HAエンティティ設定を使用）');
            return;
        }
        const options = voices.map(voice => ({
            value: voice.voice_id,
            label: voice.name ? `${voice.name} (${voice.voice_id})` : voice.voice_id
        }));
        const selected = options.some(option => option.value === preferredVoice) ? preferredVoice : '';
        if (!selected) delete (ttsSelectionsDraft[entity] || {}).voice;
        replaceSelectOptions(select, options, selected, '（HAエンティティ設定を使用）');
        select.disabled = false;
    } catch (err) {
        if (generation === ttsLoadGeneration && entity === activeTtsEntity) {
            group.style.display = '';
            replaceSelectOptions(select, [], '', '（取得できませんでした）');
            if (error) error.classList.add('visible');
        }
    } finally {
        if (generation === ttsLoadGeneration) setTtsLoading('voice', false);
    }
}


async function loadTtsConfiguration(entity) {
    const generation = ++ttsLoadGeneration;
    const languageSelect = document.getElementById('setting-tts-language');
    const languageError = document.getElementById('tts-language-error');
    const voiceGroup = document.getElementById('tts-voice-group');
    if (!languageSelect) return;
    if (languageError) languageError.classList.remove('visible');
    if (voiceGroup) voiceGroup.style.display = 'none';
    if (!entity) {
        replaceSelectOptions(languageSelect, [], '', '（TTSエンティティ未設定）');
        languageSelect.disabled = true;
        return;
    }
    languageSelect.disabled = true;
    setTtsLoading('language', true);
    try {
        const data = await getTtsInfo(entity);
        if (generation !== ttsLoadGeneration || entity !== activeTtsEntity) return;
        const languages = Array.isArray(data.languages) ? data.languages : [];
        const selection = ttsSelectionsDraft[entity] || {};
        const selectedLanguage = languages.includes(selection.language) ? selection.language : '';
        if (!selectedLanguage) {
            delete selection.language;
            delete selection.voice;
        }
        if (Object.keys(selection).length) ttsSelectionsDraft[entity] = selection;
        else delete ttsSelectionsDraft[entity];
        replaceSelectOptions(
            languageSelect,
            languages.map(language => ({ value: language, label: language })),
            selectedLanguage,
            '（HAエンティティ設定を使用）'
        );
        languageSelect.disabled = false;
        await loadTtsVoices(entity, selectedLanguage, selection.voice || '', generation);
    } catch (err) {
        if (generation === ttsLoadGeneration && entity === activeTtsEntity) {
            replaceSelectOptions(languageSelect, [], '', '（取得できませんでした）');
            if (languageError) languageError.classList.add('visible');
        }
    } finally {
        if (generation === ttsLoadGeneration) setTtsLoading('language', false);
    }
}


async function renderSettingsForm() {
    if (!prefsData) return;

    const nameEl = document.getElementById('setting-character-name');
    if (nameEl) nameEl.value = prefsData.character_name || 'Claude';
    document.getElementById('setting-character').value = characterData || "";
    const homePolicyEl = document.getElementById('setting-home-policy');
    if (homePolicyEl) homePolicyEl.value = homePolicyData || "";
    const extraContextEl = document.getElementById('setting-extra-context');
    if (extraContextEl) {
        extraContextEl.value = extraContextData || "";
    }
    const sttProvider = prefsData.stt_provider || '';
    await loadSttProviders(sttProvider);
    // まず言語一覧を読み込んでからセットする
    await loadSttLanguages(sttProvider);
    ttsSelectionsDraft = JSON.parse(JSON.stringify(
        prefsData.tts_selections && typeof prefsData.tts_selections === 'object'
            ? prefsData.tts_selections
            : {}
    ));
    activeTtsEntity = prefsData.tts_entity || '';
    await loadTtsEntities(activeTtsEntity);
    await loadTtsConfiguration(activeTtsEntity);
    const sttLangSel = document.getElementById('setting-stt-language');
    if (sttLangSel) {
        sttLangSel.value = prefsData.stt_language || 'ja-JP';
    }
    const speakersTbody = document.getElementById('speakers-tbody');
    if (speakersTbody) speakersTbody.innerHTML = '';
    const spk = prefsData.speakers;
    if (Array.isArray(spk)) {
        spk.forEach(item => createSpeakerRow(item));
    } else if (spk && typeof spk === 'object') {
        Object.entries(spk).forEach(([roomName, config]) => {
            createSpeakerRow({ room: roomName, ...config });
        });
    }

    renderCameraList(prefsData.cameras || []);
    renderMicList(prefsData.mics || []);
    renderMediaList('video', prefsData.video_media || []);
    renderMediaList('audio', prefsData.audio_media || []);


    const entitiesList = document.getElementById('entities-list');
    if (entitiesList) {
        entitiesList.innerHTML = '';
        if (prefsData.entities && Array.isArray(prefsData.entities)) {
            prefsData.entities.forEach(ent => {
                createEntityCard(ent);
            });
        }
    }

    renderProjectionTargetList(prefsData.projection_targets || []);

    initDropdownOptions('setting-presence-entity', 'input_boolean,binary_sensor,device_tracker,person', prefsData.presence?.entity);

    const loopSchedule = prefsData.loop_schedule || {};
    const loopIntervalMinEl = document.getElementById('setting-loop-interval-min');
    if (loopIntervalMinEl) loopIntervalMinEl.value = Math.round((loopSchedule.loop_interval ?? 1800) / 60);
    const loopDayProbEl = document.getElementById('setting-loop-day-prob');
    if (loopDayProbEl) loopDayProbEl.value = loopSchedule.day_probability ?? 100;
    const loopLateProbEl = document.getElementById('setting-loop-late-prob');
    if (loopLateProbEl) loopLateProbEl.value = loopSchedule.late_probability ?? 30;
    const loopNightProbEl = document.getElementById('setting-loop-night-prob');
    if (loopNightProbEl) loopNightProbEl.value = loopSchedule.night_probability ?? 10;
    const loopMinProbEl = document.getElementById('setting-loop-min-prob');
    if (loopMinProbEl) loopMinProbEl.value = loopSchedule.min_probability ?? 0;

    const httpPostEnabledEl = document.getElementById('http-post-enabled-toggle');
    if (httpPostEnabledEl) httpPostEnabledEl.checked = !!prefsData.http_post_enabled;

    const cameraHistoryEnabledEl = document.getElementById('setting-camera-history-enabled');
    const cameraHistoryMinutesEl = document.getElementById('setting-camera-history-minutes');
    const isCamHistEnabled = !!prefsData.camera_history_enabled;
    if (cameraHistoryEnabledEl) cameraHistoryEnabledEl.checked = isCamHistEnabled;
    if (cameraHistoryMinutesEl) {
        const minVal = parseInt(prefsData.camera_history_minutes, 10);
        cameraHistoryMinutesEl.value = isNaN(minVal) ? 10 : minVal;
        cameraHistoryMinutesEl.disabled = !isCamHistEnabled;
    }

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
    
    updateDynamicFeaturesUI();
    renderOtherFeaturesCatalog();
    updateSingSpeakerUI();

    // フォームの入力変更を監視して Dirty フラグを設定
    const form = document.getElementById('settings-form');
    if (form) {
        form.addEventListener('input', () => { isSettingsDirty = true; });
        form.addEventListener('change', () => { isSettingsDirty = true; });
    }
    isSettingsDirty = false;
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

    if (tabName === 'experimental') {
        // ハーネス バージョン情報の読み込み (タブを開いた時点で ?check=1 で問い合わせる)
        fetchHarnessUpdateStatus(true);
    }

    activeSettingsTab = tabName;

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
    const tabOther = document.getElementById('settings-tab-other');
    const tabExperimental = document.getElementById('settings-tab-experimental');
    const tabGames = document.getElementById('settings-tab-games');

    if (tabGeneral) tabGeneral.style.display = tabName === 'general' ? 'block' : 'none';
    if (tabIo) tabIo.style.display = tabName === 'io' ? 'block' : 'none';
    if (tabDevices) tabDevices.style.display = tabName === 'devices' ? 'block' : 'none';
    if (tabAdvanced) tabAdvanced.style.display = tabName === 'advanced' ? 'block' : 'none';
    if (tabOther) tabOther.style.display = tabName === 'other' ? 'block' : 'none';
    if (tabExperimental) tabExperimental.style.display = tabName === 'experimental' ? 'block' : 'none';
    if (tabGames) tabGames.style.display = tabName === 'games' ? 'block' : 'none';

    const noSaveBar = ['other', 'games', 'experimental'];
    const actionBar = document.querySelector('.settings-action-bar');
    if (actionBar) actionBar.style.display = noSaveBar.includes(tabName) ? 'none' : '';
    
    if (tabName === 'other') {
        voicevoxSongStatusLoaded = false;
        renderOtherFeaturesCatalog();
    }
    if (tabName === 'games') {
        await loadGames();
    }
}

async function loadGames() {
    const container = document.getElementById('games-list');
    if (!container) return;
    const nameSpan = document.getElementById('games-agent-name');
    if (nameSpan) nameSpan.textContent = (prefsData && prefsData.character_name) || 'エージェント';
    container.innerHTML = '<p class="loading-text">読み込み中...</p>';
    try {
        const res = await fetch(`${base}/api/games`);
        const data = await res.json();
        renderGames(data.games || []);
    } catch (e) {
        container.innerHTML = '<p class="error-text">読み込みに失敗しました</p>';
    }
}

function renderGames(games) {
    const container = document.getElementById('games-list');
    if (!container) return;
    if (!games.length) {
        container.innerHTML = '<p class="section-desc">ゲームが見つかりません</p>';
        return;
    }
    container.innerHTML = games.map(g => {
        const needsInstall = g.requires && g.requires.length && !g.model_installed;
        let btn = '';
        if (g.enabled) {
            btn = `<button type="button" class="btn-toggle btn-disable" onclick="toggleGame('${g.id}', false)">${g.requires && g.requires.length ? 'アンインストール' : '無効にする'}</button>`;
        } else if (needsInstall) {
            btn = `<button type="button" class="btn-toggle btn-enable" onclick="installGame('${g.id}')">インストール</button>`;
        } else {
            btn = `<button type="button" class="btn-toggle btn-enable" onclick="toggleGame('${g.id}', true)">有効にする</button>`;
        }
        return `
        <div class="game-card ${g.enabled ? 'enabled' : 'disabled'}" id="game-card-${g.id}">
            <div class="game-card-header">
                <div class="game-card-info">
                    <span class="game-name">${g.name}</span>
                    ${g.bundled ? '<span class="game-badge bundled">同梱</span>' : ''}
                    <span class="game-badge ${g.enabled ? 'active' : 'inactive'}">${g.enabled ? '有効' : '無効'}</span>
                </div>
                ${btn}
            </div>
            <p class="game-description">${g.description}</p>
            ${needsInstall ? `<p class="game-requires">必要: ${g.requires.join('、')}</p>` : ''}
        </div>`;
    }).join('');
}

async function toggleGame(id, enabled) {
    if (!enabled && id === 'wordvec_race') {
        if (!confirm('chiVeモデルを削除してアンインストールします。よろしいですか？')) return;
        try {
            const res = await fetch(`${base}/api/games/uninstall`, {method: 'POST'});
            const data = await res.json();
            if (!data.ok) alert('アンインストール失敗: ' + (data.error || '不明なエラー'));
        } catch (e) {
            alert('通信エラーが発生しました');
        }
        await loadGames();
        return;
    }
    try {
        const res = await fetch(`${base}/api/games/toggle`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({id, enabled})
        });
        const data = await res.json();
        if (data.ok) {
            await loadGames();
        } else {
            alert('切り替えに失敗しました: ' + (data.error || '不明なエラー'));
        }
    } catch (e) {
        alert('通信エラーが発生しました');
    }
}

async function installGame(id) {
    const card = document.getElementById(`game-card-${id}`);
    if (card) card.querySelector('.btn-toggle').disabled = true;
    try {
        await fetch(`${base}/api/games/install`, {method: 'POST'});
    } catch (e) {
        alert('インストール開始に失敗しました');
        await loadGames();
        return;
    }
    const poll = setInterval(async () => {
        try {
            const res = await fetch(`${base}/api/games/install-status`);
            const data = await res.json();
            const card = document.getElementById(`game-card-${id}`);
            if (card) {
                const req = card.querySelector('.game-requires');
                if (req) req.textContent = data.message || '';
            }
            if (data.status === 'done') {
                clearInterval(poll);
                await fetch(`${base}/api/games/toggle`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({id, enabled: true})
                });
                await loadGames();
            } else if (data.status === 'error') {
                clearInterval(poll);
                alert('インストール失敗: ' + (data.message || '不明なエラー'));
                await loadGames();
            }
        } catch (e) {
            clearInterval(poll);
            await loadGames();
        }
    }, 2000);
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
    const speakers = [];
    document.querySelectorAll('#speakers-tbody .speaker-item').forEach(tr => {
        const room = tr.dataset.room || '';
        if (!room) return;
        const item = {
            room,
            entity: tr.dataset.entity || undefined,
            label: tr.dataset.label || undefined,
            note: tr.dataset.note || undefined,
        };
        // undefinedキーを除去
        Object.keys(item).forEach(k => item[k] === undefined && delete item[k]);
        speakers.push(item);
    });

    const cameras = [];
    document.querySelectorAll('#cameras-tbody .camera-item').forEach(tr => {
        const source = tr.dataset.source || '';
        if (source) {
            const camObj = { source };
            if (tr.dataset.room) camObj.room = tr.dataset.room;
            if (tr.dataset.entity) camObj.entity = tr.dataset.entity;
            if (tr.dataset.label) camObj.label = tr.dataset.label;
            if (tr.dataset.note) camObj.note = tr.dataset.note;
            cameras.push(camObj);
        }
    });

    const entities = [];
    document.querySelectorAll('.entity-item').forEach(tr => {
        const entity_id = tr.dataset.entityId || '';
        const name = (tr.dataset.name || '').trim();
        const note = (tr.dataset.note || '').trim();
        if (entity_id) {
            const entObj = { name, entity_id };
            if (note) entObj.note = note;
            entities.push(entObj);
        }
    });

    const presence = {
        entity: document.getElementById('setting-presence-entity').value
    };

    const loopIntervalMinRaw = parseFloat(document.getElementById('setting-loop-interval-min')?.value);
    const loopSchedule = !isNaN(loopIntervalMinRaw) && loopIntervalMinRaw > 0 ? {
        loop_interval: Math.round(loopIntervalMinRaw * 60),
        day_probability: parseInt(document.getElementById('setting-loop-day-prob')?.value, 10) || 0,
        late_probability: parseInt(document.getElementById('setting-loop-late-prob')?.value, 10) || 0,
        night_probability: parseInt(document.getElementById('setting-loop-night-prob')?.value, 10) || 0,
        min_probability: parseInt(document.getElementById('setting-loop-min-prob')?.value, 10) || 0,
    } : (prefsData.loop_schedule || undefined);

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
        if (card.querySelector('.sensor-context-loop').checked) contexts.push('loop');
        if (card.querySelector('.sensor-context-chat').checked) contexts.push('chat');

        const items = [];
        const itemRows = card.querySelectorAll('.sensor-item-row');
        itemRows.forEach(row => {
            const label = (row.dataset.label || '').trim();
            const isTemplate = row.dataset.isTemplate === 'true';
            const note = (row.dataset.note || '').trim();

            const itemObj = {};
            if (label) itemObj.label = label;
            if (note) itemObj.note = note;

            if (isTemplate) {
                const template = (row.dataset.template || '').trim();
                if (template) {
                    itemObj.template = template;
                    items.push(itemObj);
                }
            } else {
                const entity = row.dataset.entity || '';
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

    const projection_targets = [];
    document.querySelectorAll('.projection-target-item').forEach(tr => {
        const displayName = (tr.dataset.displayName || '').trim();
        const id = tr.dataset.id || '';
        const room = tr.dataset.room || null;
        if (displayName && id) {
            projection_targets.push({ id, display_name: displayName, room: room || null });
        }
    });

    const mics = getMicsFromUI();
    const video_media = getMediaFromUI('video');
    const audio_media = getMediaFromUI('audio');
    const stt_provider = document.getElementById('setting-stt-provider')?.value?.trim() || null;
    const stt_language = document.getElementById('setting-stt-language')?.value?.trim() || 'ja-JP';
    const tts_entity = document.getElementById('setting-tts-entity')?.value?.trim() || null;
    const ttsLanguage = document.getElementById('setting-tts-language')?.value?.trim() || '';
    const ttsVoice = document.getElementById('setting-tts-voice')?.value?.trim() || '';
    if (tts_entity) {
        const selection = {};
        if (ttsLanguage) selection.language = ttsLanguage;
        if (ttsLanguage && ttsVoice) selection.voice = ttsVoice;
        if (Object.keys(selection).length) ttsSelectionsDraft[tts_entity] = selection;
        else delete ttsSelectionsDraft[tts_entity];
    }
    const singSpeakerSelect = document.getElementById('setting-sing-speaker');
    let sing_speaker = undefined;
    if (singSpeakerSelect && singSpeakerSelect.value) {
        const styleId = parseInt(singSpeakerSelect.value, 10);
        const selectedSinger = voicevoxSongSingers.find(s => s.style_id === styleId);
        if (selectedSinger) {
            sing_speaker = {
                name: selectedSinger.name,
                style_id: selectedSinger.style_id
            };
        }
    }

    const {
        audio_sources: _audioSources,
        sing_speaker: _singSpeaker,
        tts_options: _ttsOptions,
        tts_provider: _ttsProvider,
        wake_words: _wakeWords,
        wake_ack: _wakeAck,
        ...prefsBase
    } = prefsData || {};

    const returnObj = {
        ...prefsBase,
        character_name: (document.getElementById('setting-character-name')?.value || '').trim() || 'Claude',
        cameras,
        mics,
        video_media,
        audio_media,
        stt_provider,
        stt_language,
        tts_entity,
        tts_selections: ttsSelectionsDraft,
        speakers,
        entities,
        presence,
        policies,
        sensors,
        projection_targets,
        loop_schedule: loopSchedule
    };

    const cameraHistoryEnabledEl = document.getElementById('setting-camera-history-enabled');
    const cameraHistoryMinutesEl = document.getElementById('setting-camera-history-minutes');
    if (cameraHistoryEnabledEl) {
        returnObj.camera_history_enabled = cameraHistoryEnabledEl.checked;
    }
    if (cameraHistoryMinutesEl) {
        const parsed = parseInt(cameraHistoryMinutesEl.value, 10);
        returnObj.camera_history_minutes = isNaN(parsed) ? 10 : Math.min(60, Math.max(1, parsed));
    }

    if (sing_speaker) {
        returnObj.sing_speaker = sing_speaker;
    }

    return returnObj;
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

async function getRoomGraphData() {
    if (window.roomGraphData && typeof window.roomGraphData === 'object') {
        return window.roomGraphData;
    }
    if (window.roomGraphDataPromise) {
        return window.roomGraphDataPromise;
    }

    window.roomGraphDataPromise = fetch(`${base}/api/body/rooms`)
        .then(r => {
            if (!r.ok) {
                throw new Error(`room graph request failed: ${r.status}`);
            }
            return r.json();
        })
        .then(data => {
            window.roomGraphData = data && typeof data === 'object' ? data : {};
            return window.roomGraphData;
        })
        .catch(err => {
            console.warn('[Settings] room graph fetch failed, falling back to text input', err);
            window.roomGraphData = null;
            return null;
        });

    return window.roomGraphDataPromise;
}

function populateRoomSelect(selectEl, selectedRoom, customClass = 'pt-room') {
    if (!selectEl) return;
    const fallbackToText = () => {
        if (selectEl.dataset.fallbackInput === 'true') return;
        const input = document.createElement('input');
        input.type = 'text';
        input.className = `${selectEl.className} ${customClass}`;
        input.placeholder = '例: study';
        input.value = selectedRoom || '';
        input.dataset.fallbackInput = 'true';
        selectEl.replaceWith(input);
    };

    const applyRooms = data => {
        const rooms = data?.rooms || {};
        const entries = Object.entries(rooms);
        if (!entries.length) {
            fallbackToText();
            return;
        }

        selectEl.innerHTML = '<option value="">指定なし（モバイル等）</option>';
        entries.forEach(([roomId, roomInfo]) => {
            const opt = document.createElement('option');
            opt.value = roomId;
            opt.textContent = roomInfo?.display_name || roomId;
            if (roomId === selectedRoom) opt.selected = true;
            selectEl.appendChild(opt);
        });

        if (!selectedRoom) {
            selectEl.value = '';
        } else if (!Array.from(selectEl.options).some(opt => opt.value === selectedRoom)) {
            fallbackToText();
        }
    };

    if (window.roomGraphData && typeof window.roomGraphData === 'object') {
        applyRooms(window.roomGraphData);
        return;
    }

    getRoomGraphData().then(data => {
        if (data) {
            applyRooms(data);
        } else {
            fallbackToText();
        }
    });
}

function renderProjectionTargetList(targets) {
    const listEl = document.getElementById('projection-targets-list');
    if (!listEl) return;
    listEl.innerHTML = '';
    if (Array.isArray(targets)) {
        targets.forEach(t => addProjectionTargetRow(t, false));
    }
}

function addProjectionTargetRow(target = {}, isNew = false) {
    const listEl = document.getElementById('projection-targets-list');
    if (!listEl) return;
    const tr = document.createElement('tr');
    tr.className = 'projection-target-item';

    const id = target.id || '';
    const displayName = target.display_name || '';
    const room = target.room || '';

    // Set dataset attributes
    tr.dataset.id = id;
    tr.dataset.displayName = displayName;
    tr.dataset.room = room;

    tr.innerHTML = `
        <td>
            <div class="view-mode-element font-mono">${esc(id || '(新規デバイス)')}</div>
        </td>
        <td>
            <div class="view-mode-element">${esc(displayName || '')}</div>
        </td>
        <td>
            <div class="view-mode-element room-display">${esc(room || '指定なし')}</div>
        </td>
        <td style="text-align: center; vertical-align: middle;">
            <button type="button" class="btn-edit" onclick="openEditModal('projection', this.closest('tr'))" title="編集">✏️</button>
        </td>
        <td style="text-align: center; vertical-align: middle;">
            <button type="button" class="btn-remove-icon" onclick="if(confirm('このデバイスを削除しますか？')) { this.closest('.projection-target-item').remove(); isSettingsDirty = true; }" title="削除">✕</button>
        </td>
    `;

    listEl.appendChild(tr);

    if (room) {
        getRoomGraphData().then(data => {
            const display = data?.rooms?.[room]?.display_name || room;
            const el = tr.querySelector('.room-display');
            if (el) el.textContent = display;
        });
    }

    if (isNew) {
        openEditModal('projection', tr);
    }
    return tr;
}

function populateSpeakerHaEntityDropdown(selectEl, currentValue) {
    selectEl.innerHTML = '<option value="">(選択してください)</option>';
    const entities = entityList || {};
    const mpList = (entities['media_player'] || []).map(e => ({...e, _domain: 'media_player'}));

    if (mpList.length) {
        const group = document.createElement('optgroup');
        group.label = 'スピーカー (media_player)';
        mpList.sort((a, b) => {
            const areaA = a.area || '';
            const areaB = b.area || '';
            if (areaA !== areaB) return areaA.localeCompare(areaB, 'ja');
            return (a.friendly_name || '').localeCompare(b.friendly_name || '', 'ja');
        });
        mpList.forEach(ent => {
            const opt = document.createElement('option');
            opt.value = ent.entity_id;
            const areaStr = ent.area ? `[${ent.area}] ` : '';
            opt.textContent = `${areaStr}${ent.friendly_name} (${ent.entity_id})`;
            if (ent.entity_id === currentValue) opt.selected = true;
            group.appendChild(opt);
        });
        selectEl.appendChild(group);
    }
    
    if (currentValue && !Array.from(selectEl.options).some(o => o.value === currentValue)) {
        const opt = document.createElement('option');
        opt.value = currentValue;
        opt.textContent = `⚠️ ${currentValue} (未発見)`;
        opt.selected = true;
        selectEl.appendChild(opt);
    }
}
window.populateSpeakerHaEntityDropdown = populateSpeakerHaEntityDropdown;

function createSpeakerRow(item = {}) {
    const tbody = document.getElementById('speakers-tbody');
    if (!tbody) return;
    const tr = document.createElement('tr');
    tr.className = 'speaker-item';
    setOriginalRowData(tr, item);

    const room = item.room || '';
    const label = item.label || '';
    const entity = item.entity || '';
    const note = item.note || '';

    tr.dataset.room = room;
    tr.dataset.label = label;
    tr.dataset.entity = entity;
    tr.dataset.note = note;

    tr.innerHTML = `
        <td style="font-weight:600;">${esc(room || '（未設定）')}</td>
        <td style="font-size:12px;">${esc(entity || '（未設定）')}</td>
        <td>${esc(label)}</td>
        <td style="text-align:center;">
            <button type="button" class="btn-icon" title="編集"
                    onclick="openEditModal('speaker', this.closest('tr'))">✏️</button>
        </td>
        <td style="text-align:center;">
            <button type="button" class="btn-icon btn-remove-icon" title="削除"
                    onclick="if(confirm('このスピーカーを削除しますか？')) this.closest('tr').remove()">✕</button>
        </td>
    `;
    tbody.appendChild(tr);
}

function addSpeakerRow() {
    createSpeakerRow({});
}

function addSpeakerRowAndOpen() {
    createSpeakerRow({});
    const rows = document.querySelectorAll('#speakers-tbody .speaker-item');
    const last = rows[rows.length - 1];
    if (last) {
        const accordion = document.getElementById('accordion-speakers');
        if (accordion) openAccordion(accordion);
        openEditModal('speaker', last);
    }
}

function cloneOriginalRowData(data) {
    return data && typeof data === 'object' ? { ...data } : {};
}

function getOriginalRowData(tr) {
    return cloneOriginalRowData(tr?._originalData);
}

function setOriginalRowData(tr, data) {
    tr._originalData = cloneOriginalRowData(data);
}

function mergeRowData(tr, edits = {}) {
    return { ...getOriginalRowData(tr), ...edits };
}

function cleanRowData(data) {
    const out = {};
    Object.entries(data).forEach(([key, value]) => {
        if (value !== undefined) out[key] = value;
    });
    return out;
}

function cleanMicRowData(data) {
    const out = cleanRowData(data);
    delete out.stt_enabled;
    delete out.stt_retention_hours;
    delete out.wake_word_enabled;
    delete out.background_hearing_enabled;
    return out;
}

function createCameraRow(cam = {}) {
    const tbody = document.getElementById('cameras-tbody');
    if (!tbody) return;
    const tr = document.createElement('tr');
    tr.className = 'camera-item';
    setOriginalRowData(tr, cam);
    tr.dataset.room = cam.room || '';
    tr.dataset.source = cam.source || '';
    tr.dataset.entity = cam.entity || '';
    tr.dataset.label = cam.label || '';
    // ptz は {left,right,up,down} オブジェクト。dataset(文字列)化すると "[object Object]" に
    // 壊れるため経由させない。_originalData で無編集round-tripする（編集はJSONタブ）。
    tr.dataset.note = cam.note || '';
    tr.innerHTML = `
        <td>${esc(cam.room || '')}</td>
        <td>${esc(cam.source || '（未設定）')}</td>
        <td>${esc(cam.label || '')}</td>
        <td style="text-align:center;">
            <button type="button" class="btn-icon" title="編集"
                    onclick="openEditModal('camera', this.closest('tr'))">✏️</button>
        </td>
        <td style="text-align:center;">
            <button type="button" class="btn-icon btn-remove-icon" title="削除"
                    onclick="if(confirm('このカメラを削除しますか？')) this.closest('tr').remove()">✕</button>
        </td>
    `;
    tbody.appendChild(tr);
}

function addCameraRowAndOpen() {
    createCameraRow();
    const rows = document.querySelectorAll('#cameras-tbody .camera-item');
    const last = rows[rows.length - 1];
    if (last) {
        const accordion = document.getElementById('accordion-cameras');
        if (accordion) openAccordion(accordion);
        openEditModal('camera', last);
    }
}

function renderCameraList(cameras) {
    const tbody = document.getElementById('cameras-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    (Array.isArray(cameras) ? cameras : []).forEach(cam => createCameraRow(cam));
}

function getCamerasFromUI() {
    const items = [];
    document.querySelectorAll('#cameras-tbody .camera-item').forEach(tr => {
        const source = (tr.dataset.source || '').trim();
        if (!source) return;
        const edits = { source };
        const room = (tr.dataset.room || '').trim();
        const entity = (tr.dataset.entity || '').trim();
        const label = (tr.dataset.label || '').trim();
        const note = (tr.dataset.note || '').trim();
        if (room) edits.room = room;
        if (entity) edits.entity = entity;
        if (label) edits.label = label;
        if (note) edits.note = note;
        // ptz は edits に入れない（_originalData のオブジェクトを mergeRowData が保持）
        items.push(cleanRowData(mergeRowData(tr, edits)));
    });
    return items;
}

function createMicRow(mic = {}) {
    const tbody = document.getElementById('mics-tbody');
    if (!tbody) return;
    const tr = document.createElement('tr');
    tr.className = 'mic-item';
    setOriginalRowData(tr, mic);
    tr.dataset.room = mic.room || '';
    tr.dataset.source = mic.source || '';
    tr.dataset.entity = mic.entity || '';
    tr.dataset.label = mic.label || '';
    tr.dataset.note = mic.note || '';
    tr.innerHTML = `
        <td>${esc(mic.room || '')}</td>
        <td>${esc(mic.source || '（未設定）')}</td>
        <td>${esc(mic.label || '')}</td>
        <td style="text-align:center;">
            <button type="button" class="btn-icon" title="編集"
                    onclick="openEditModal('mic', this.closest('tr'))">✏️</button>
        </td>
        <td style="text-align:center;">
            <button type="button" class="btn-icon btn-remove-icon" title="削除"
                    onclick="if(confirm('このマイクを削除しますか？')) this.closest('tr').remove()">✕</button>
        </td>
    `;
    tbody.appendChild(tr);
}

function addMicRowAndOpen() {
    createMicRow();
    const rows = document.querySelectorAll('#mics-tbody .mic-item');
    const last = rows[rows.length - 1];
    if (last) {
        const accordion = document.getElementById('accordion-mics');
        if (accordion) openAccordion(accordion);
        openEditModal('mic', last);
    }
}

function renderMicList(mics) {
    const tbody = document.getElementById('mics-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    (Array.isArray(mics) ? mics : []).forEach(mic => createMicRow(mic));
}

function getMicsFromUI() {
    const items = [];
    document.querySelectorAll('#mics-tbody .mic-item').forEach(tr => {
        const source = (tr.dataset.source || '').trim();
        if (!source) return;
        const edits = { source };
        const room = (tr.dataset.room || '').trim();
        const entity = (tr.dataset.entity || '').trim();
        const label = (tr.dataset.label || '').trim();
        const note = (tr.dataset.note || '').trim();
        if (room) edits.room = room;
        if (entity) edits.entity = entity;
        if (label) edits.label = label;
        if (note) edits.note = note;
        items.push(cleanMicRowData(mergeRowData(tr, edits)));
    });
    return items;
}

function createMediaRow(kind, media = {}) {
    const tbody = document.getElementById(`${kind}-media-tbody`);
    if (!tbody) return;
    const tr = document.createElement('tr');
    tr.className = `${kind}-media-item`;
    setOriginalRowData(tr, media);
    tr.dataset.id = media.id || '';
    tr.dataset.source = media.source || '';
    tr.dataset.room = media.room || '';
    tr.dataset.label = media.label || '';
    tr.dataset.note = media.note || '';
    tr.innerHTML = `
        <td>${esc(media.id || '（未設定）')}</td>
        <td>${esc(media.source || '（未設定）')}</td>
        <td>${esc(media.room || '')}</td>
        <td>${esc(media.label || '')}</td>
        <td style="text-align:center;">
            <button type="button" class="btn-icon" title="編集"
                    onclick="openEditModal('${kind}-media', this.closest('tr'))">✏️</button>
        </td>
        <td style="text-align:center;">
            <button type="button" class="btn-icon btn-remove-icon" title="削除"
                    onclick="if(confirm('この${kind === 'video' ? '映像' : '音声'}ソースを削除しますか？')) this.closest('tr').remove()">✕</button>
        </td>
    `;
    tbody.appendChild(tr);
}

function renderMediaList(kind, items) {
    const tbody = document.getElementById(`${kind}-media-tbody`);
    if (!tbody) return;
    tbody.innerHTML = '';
    (Array.isArray(items) ? items : []).forEach(item => createMediaRow(kind, item));
}

function getMediaFromUI(kind) {
    const items = [];
    document.querySelectorAll(`.${kind}-media-item`).forEach(tr => {
        const id = (tr.dataset.id || '').trim();
        const source = (tr.dataset.source || '').trim();
        if (!id || !source) return;
        const edits = { id, source };
        const room = (tr.dataset.room || '').trim();
        const label = (tr.dataset.label || '').trim();
        const note = (tr.dataset.note || '').trim();
        if (room) edits.room = room;
        if (label) edits.label = label;
        if (note) edits.note = note;
        items.push(cleanRowData(mergeRowData(tr, edits)));
    });
    return items;
}

function addVideoMediaRowAndOpen() {
    createMediaRow('video');
    const rows = document.querySelectorAll('#video-media-tbody .video-media-item');
    const last = rows[rows.length - 1];
    if (last) {
        const accordion = document.getElementById('accordion-video-media');
        if (accordion) openAccordion(accordion);
        openEditModal('video-media', last);
    }
}

function addAudioMediaRowAndOpen() {
    createMediaRow('audio');
    const rows = document.querySelectorAll('#audio-media-tbody .audio-media-item');
    const last = rows[rows.length - 1];
    if (last) {
        const accordion = document.getElementById('accordion-audio-media');
        if (accordion) openAccordion(accordion);
        openEditModal('audio-media', last);
    }
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

// --- Accordion & Inline Edit Helpers ---
function toggleAccordion(header) {
    const section = header.closest('.accordion-section');
    const content = section.querySelector('.accordion-content');
    const icon = section.querySelector('.accordion-icon');
    
    if (content.style.display === 'block') {
        content.style.display = 'none';
        icon.textContent = '▶';
    } else {
        content.style.display = 'block';
        icon.textContent = '▼';
    }
}

function addEntityRowAndOpen(btn) {
    const section = document.getElementById('accordion-entities');
    const content = section.querySelector('.accordion-content');
    const icon = section.querySelector('.accordion-icon');
    content.style.display = 'block';
    icon.textContent = '▼';
    addEntityRow();
}

function addSensorGroupAndOpen(btn) {
    const section = document.getElementById('accordion-sensors');
    const content = section.querySelector('.accordion-content');
    const icon = section.querySelector('.accordion-icon');
    content.style.display = 'block';
    icon.textContent = '▼';
    addSensorGroup();
}

function addProjectionTargetRowAndOpen(btn) {
    const section = document.getElementById('accordion-projection-targets');
    const content = section.querySelector('.accordion-content');
    const icon = section.querySelector('.accordion-icon');
    content.style.display = 'block';
    icon.textContent = '▼';
    addProjectionTargetRow();
}

function createEntityCard(ent = { name: '', entity_id: '', note: '' }, isNew = false) {
    const entitiesList = document.getElementById('entities-list');
    if (!entitiesList) return;
    const tr = document.createElement('tr');
    tr.className = 'entity-item';
    
    // Set dataset attributes
    tr.dataset.entityId = ent.entity_id || '';
    tr.dataset.name = ent.name || '';
    tr.dataset.note = ent.note || '';

    tr.innerHTML = `
        <td>
            <div class="view-mode-element font-mono">${esc(ent.entity_id || '(未選択)')}</div>
        </td>
        <td>
            <div class="view-mode-element">${esc(ent.name || '')}</div>
        </td>
        <td>
            <div class="view-mode-element">${esc(ent.note || '')}</div>
        </td>
        <td style="text-align: center; vertical-align: middle;">
            <button type="button" class="btn-edit" onclick="openEditModal('entity', this.closest('tr'))" title="編集">✏️</button>
        </td>
        <td style="text-align: center; vertical-align: middle;">
            <button type="button" class="btn-remove-icon" onclick="if(confirm('この家電を削除しますか？')) { this.closest('.entity-item').remove(); isSettingsDirty = true; }" title="削除">✕</button>
        </td>
    `;

    entitiesList.appendChild(tr);

    if (isNew) {
        openEditModal('entity', tr);
    }
    return tr;
}

function addEntityRow() {
    createEntityCard({ name: '', entity_id: '', note: '' }, true);
}

// --- Edit Modal Functions ---
function openAccordion(accordion) {
    const content = accordion.querySelector('.accordion-content');
    const icon = accordion.querySelector('.accordion-icon');
    if (content) content.style.display = 'block';
    if (icon) icon.textContent = '▼';
}

function updateCameraModalEntityState(modal) {
    const selectEl = modal.querySelector('.camera-source-select-modal');
    const sourceInput = modal.querySelector('.camera-source-modal');
    const entityInput = modal.querySelector('.camera-entity-modal');
    const entityHint = modal.querySelector('.camera-entity-hint-modal');
    if (!entityInput) return;

    // ドロップダウンでHAエンティティが選ばれているか
    const selectVal = selectEl ? selectEl.value : '';
    const isHaSelect = selectVal && selectVal !== '__custom__';
    const sourceVal = isHaSelect ? selectVal : (sourceInput ? sourceInput.value.trim() : '');

    if (sourceVal.includes('.')) {
        entityInput.value = sourceVal;
        entityInput.readOnly = true;
        if (entityHint) entityHint.textContent = 'HAカメラから自動設定されるため変更できません。';
    } else {
        entityInput.readOnly = false;
        if (entityHint) entityHint.textContent = 'HAカメラは entity_id（camera.xxx）、go2rtcは任意の短いID。電脳体として侵入するときに使います。';
    }
}

// Speaker selection is HA media_player entity only

// --- Edit Modal Functions ---
function openEditModal(type, tr) {
    _currentEditTr = tr;
    _currentEditType = type;
    
    const modal = document.getElementById('edit-modal');
    const titleEl = document.getElementById('edit-modal-title');
    const bodyEl = document.getElementById('edit-modal-body');
    
    if (!modal || !titleEl || !bodyEl) return;
    
    bodyEl.innerHTML = '';
    
    if (type === 'entity') {
        titleEl.textContent = '家電の編集';
        const entityId = tr.dataset.entityId || '';
        const name = tr.dataset.name || '';
        const note = tr.dataset.note || '';
        
        bodyEl.innerHTML = `
            <div class="form-group">
                <label class="form-label">エンティティ (entity_id)</label>
                <select class="entity-eid-modal ha-entity-select-field form-input" onchange="handleEntitySelectChangeModal(this)">
                    <option value="">(ロード中...)</option>
                </select>
            </div>
            <div class="form-group">
                <label class="form-label">呼び方 (name)</label>
                <input type="text" class="entity-name-modal form-input" placeholder="例: リビングのライト" value="${esc(name)}">
            </div>
            <div class="form-group">
                <label class="form-label">備考 (note)</label>
                <input type="text" class="entity-note-modal form-input" placeholder="例: 要確認" value="${esc(note)}">
            </div>
        `;
        const select = bodyEl.querySelector('.entity-eid-modal');
        initDropdownOptions(select, ENTITY_CONTROLLABLE_DOMAINS, entityId);
        
    } else if (type === 'camera') {
        titleEl.textContent = 'カメラを編集';
        const room = tr.dataset.room || '';
        const source = tr.dataset.source || '';
        const entity = tr.dataset.entity || '';
        const label = tr.dataset.label || '';
        const note = tr.dataset.note || '';

        const isHaEntity = source && source.includes('.');
        const customSource = isHaEntity ? '' : source;

        bodyEl.innerHTML = `
            <div class="form-group">
                <label class="form-label">部屋 (room)</label>
                <input type="text" class="camera-room-modal form-input" placeholder="例: study" value="${esc(room)}">
            </div>
            <div class="form-group">
                <label class="form-label">ソース (source)</label>
                <select class="camera-source-select-modal form-input">
                    <option value="">(未選択)</option>
                    <option value="__custom__">その他のRTSPストリーム（手動入力）</option>
                </select>
                <input type="text" class="camera-source-modal form-input" placeholder="例: capture_tv または rtsp://..." value="${esc(customSource)}" style="margin-top: 6px; display: ${isHaEntity ? 'none' : 'block'};">
            </div>
            <div class="form-group">
                <label class="form-label">カメラID (entity)</label>
                <input type="text" class="camera-entity-modal form-input" placeholder="例: camera.living_room または camera_tv" value="${esc(entity)}">
                <p class="form-hint camera-entity-hint-modal" style="margin-top: 4px; font-size: 11px;"></p>
            </div>
            <div class="form-group">
                <label class="form-label">ラベル (label)</label>
                <input type="text" class="camera-label-modal form-input" placeholder="例: リビング" value="${esc(label)}">
            </div>
            <div class="form-group">
                <label class="form-label">メモ (note)</label>
                <input type="text" class="camera-note-modal form-input" placeholder="例: リビングの広角カメラ (任意)" value="${esc(note)}">
            </div>
        `;

        const selectEl = bodyEl.querySelector('.camera-source-select-modal');
        const customOpt = selectEl.querySelector('option[value="__custom__"]');
        const cameras = (entityList && entityList['camera']) || [];
        cameras.slice().sort((a, b) => {
            const areaA = a.area || '', areaB = b.area || '';
            if (areaA !== areaB) return areaA.localeCompare(areaB, 'ja');
            return (a.friendly_name || '').localeCompare(b.friendly_name || '', 'ja');
        }).forEach(ent => {
            const opt = document.createElement('option');
            opt.value = ent.entity_id;
            const areaStr = ent.area ? `[${ent.area}] ` : '';
            opt.textContent = `${areaStr}${ent.friendly_name} (${ent.entity_id})`;
            selectEl.insertBefore(opt, customOpt);
        });

        if (isHaEntity && Array.from(selectEl.options).some(o => o.value === source)) {
            selectEl.value = source;
        } else if (source) {
            selectEl.value = '__custom__';
        }

        const sourceInput = bodyEl.querySelector('.camera-source-modal');
        selectEl.addEventListener('change', () => {
            if (selectEl.value === '__custom__') {
                sourceInput.style.display = 'block';
                sourceInput.focus();
                updateCameraModalEntityState(bodyEl);
            } else if (selectEl.value) {
                sourceInput.style.display = 'none';
                sourceInput.value = selectEl.value;
                updateCameraModalEntityState(bodyEl);
            } else {
                sourceInput.style.display = 'none';
                sourceInput.value = '';
                updateCameraModalEntityState(bodyEl);
            }
        });
        sourceInput.addEventListener('input', () => updateCameraModalEntityState(bodyEl));
        updateCameraModalEntityState(bodyEl);

    } else if (type === 'mic') {
        titleEl.textContent = 'マイクを編集';
        const source = tr.dataset.source || '';
        const room = tr.dataset.room || '';
        const label = tr.dataset.label || '';
        const entity = tr.dataset.entity || '';
        const note = tr.dataset.note || '';
        bodyEl.innerHTML = `
            <div class="form-group">
                <label class="form-label">ソース (source)</label>
                <input type="text" class="mic-source-modal form-input" placeholder="例: rtsp://localhost:8554/study_mic" value="${esc(source)}">
            </div>
            <div class="form-group">
                <label class="form-label">部屋 (room)</label>
                <input type="text" class="mic-room-modal form-input" placeholder="例: study" value="${esc(room)}">
            </div>
            <div class="form-group">
                <label class="form-label">デバイスID (entity)</label>
                <input type="text" class="mic-entity-modal form-input" placeholder="デバイスID (任意)" value="${esc(entity)}">
            </div>
            <div class="form-group">
                <label class="form-label">ラベル (label)</label>
                <input type="text" class="mic-label-modal form-input" placeholder="例: スタディマイク" value="${esc(label)}">
            </div>
            <div class="form-group">
                <label class="form-label">メモ (note)</label>
                <input type="text" class="mic-note-modal form-input" placeholder="メモ (任意)" value="${esc(note)}">
            </div>
        `;

    } else if (type === 'video-media' || type === 'audio-media') {
        const isVideo = type === 'video-media';
        titleEl.textContent = isVideo ? '映像ソースを編集' : '音声ソースを編集';
        const id = tr.dataset.id || '';
        const source = tr.dataset.source || '';
        const room = tr.dataset.room || '';
        const label = tr.dataset.label || '';
        const note = tr.dataset.note || '';

        bodyEl.innerHTML = `
            <div class="form-group">
                <label class="form-label">ID</label>
                <input type="text" class="media-id-modal form-input" placeholder="例: tv_video" value="${esc(id)}">
            </div>
            <div class="form-group">
                <label class="form-label">ソース (source)</label>
                <input type="text" class="media-source-modal form-input" placeholder="例: capture_tv, rtsp://..." value="${esc(source)}">
            </div>
            <div class="form-group">
                <label class="form-label">部屋 (room, 任意)</label>
                <input type="text" class="media-room-modal form-input" placeholder="例: living" value="${esc(room)}">
            </div>
            <div class="form-group">
                <label class="form-label">ラベル (label)</label>
                <input type="text" class="media-label-modal form-input" placeholder="例: リビングテレビ" value="${esc(label)}">
            </div>
            <div class="form-group">
                <label class="form-label">メモ (note)</label>
                <input type="text" class="media-note-modal form-input" placeholder="メモ (任意)" value="${esc(note)}">
            </div>
        `;

    } else if (type === 'speaker') {
        titleEl.textContent = 'スピーカーを編集';
        const room = tr.dataset.room || '';
        const label = tr.dataset.label || '';
        const entity = tr.dataset.entity || '';
        const note = tr.dataset.note || '';

        bodyEl.innerHTML = `
            <div class="form-group">
                <label class="form-label">部屋名 (room)</label>
                <input type="text" class="speaker-room-modal form-input" placeholder="例: study" value="${esc(room)}">
            </div>
            <div class="form-group">
                <label class="form-label">HAエンティティ (media_player.xxx)</label>
                <select class="speaker-ha-entity-modal form-input">
                    <option value="">(未選択)</option>
                </select>
            </div>
            <div class="form-group">
                <label class="form-label">ラベル (label)</label>
                <input type="text" class="speaker-label-modal form-input" placeholder="例: 書斎（Nest Mini）" value="${esc(label)}">
            </div>
            <div class="form-group">
                <label class="form-label">メモ (note)</label>
                <input type="text" class="speaker-note-modal form-input" placeholder="任意" value="${esc(note)}">
            </div>
        `;

        // media_player ドロップダウンを populate
        const haEntitySelect = bodyEl.querySelector('.speaker-ha-entity-modal');
        populateSpeakerHaEntityDropdown(haEntitySelect, entity);
    } else if (type === 'sensor') {
        titleEl.textContent = 'センサーの編集';
        const label = tr.dataset.label || '';
        const entity = tr.dataset.entity || '';
        const template = tr.dataset.template || '';
        const note = tr.dataset.note || '';
        const isTemplate = tr.dataset.isTemplate === 'true';
        
        bodyEl.innerHTML = `
            <div class="form-group">
                <label class="form-label">ラベル (label)</label>
                <input type="text" class="sensor-label-modal form-input" placeholder="例: リビング" value="${esc(label)}">
            </div>
            <div class="form-group sensor-entity-container-modal" style="display: ${isTemplate ? 'none' : 'block'};">
                <label class="form-label">エンティティ</label>
                <select class="sensor-entity-modal ha-entity-select-field form-input">
                    <option value="">(ロード中...)</option>
                </select>
            </div>
            <div class="form-group sensor-template-container-modal" style="display: ${isTemplate ? 'block' : 'none'};">
                <label class="form-label">テンプレート</label>
                <input type="text" class="sensor-template-modal form-input" placeholder="Template (例: {{ states('sensor.temp') }}℃)" value="${esc(template)}">
            </div>
            <div class="checkbox-group sensor-item-mode-checkbox" style="margin-top: 4px; margin-bottom: 12px;">
                <label class="checkbox-label" style="font-size:13px;">
                    <input type="checkbox" class="sensor-is-template-modal" ${isTemplate ? 'checked' : ''} onchange="toggleSensorItemModeModal(this)"> 式(Template)
                </label>
            </div>
            <div class="form-group">
                <label class="form-label">メモ (note)</label>
                <input type="text" class="sensor-note-modal form-input" placeholder="メモ (任意)" value="${esc(note)}">
            </div>
        `;
        const select = bodyEl.querySelector('.sensor-entity-modal');
        initDropdownOptions(select, 'binary_sensor,sensor,input_boolean', entity);
        
    } else if (type === 'projection') {
        titleEl.textContent = '外部デバイスの編集';
        const id = tr.dataset.id || '';
        const slug = id.replace(/^external:\/\//, '');
        const displayName = tr.dataset.displayName || '';
        const room = tr.dataset.room || '';
        
        bodyEl.innerHTML = `
            <div class="form-group">
                <label class="form-label">IDスラグ</label>
                <div class="input-prefix-group">
                    <span class="input-prefix">external://</span>
                    <input type="text" class="pt-slug-modal form-input input-prefix-input" placeholder="device_name" value="${esc(slug)}">
                </div>
            </div>
            <div class="form-group">
                <label class="form-label">表示名</label>
                <input type="text" class="pt-name-modal form-input" placeholder="例: Astrolabe（スタディ）" value="${esc(displayName)}">
            </div>
            <div class="form-group">
                <label class="form-label">部屋</label>
                <select class="pt-room-modal form-input">
                    <option value="">指定なし（モバイル等）</option>
                </select>
            </div>
        `;
        
        const slugInput = bodyEl.querySelector('.pt-slug-modal');
        const nameInput = bodyEl.querySelector('.pt-name-modal');
        const roomSelect = bodyEl.querySelector('.pt-room-modal');
        
        if (!id) {
            slugInput.dataset.autoGenerated = 'true';
        } else {
            slugInput.dataset.autoGenerated = 'false';
        }
        
        nameInput.addEventListener('input', () => {
            if (!slugInput.value || slugInput.dataset.autoGenerated === 'true') {
                const generatedSlug = nameInput.value
                    .toLowerCase()
                    .replace(/[\s　]+/g, '_')
                    .replace(/[^\w]/g, '')
                    .replace(/^_+|_+$/g, '');
                slugInput.value = generatedSlug;
                slugInput.dataset.autoGenerated = 'true';
            }
        });
        
        slugInput.addEventListener('input', () => {
            slugInput.dataset.autoGenerated = 'false';
        });
        
        populateRoomSelect(roomSelect, room, 'pt-room-modal');
    }
    
    modal.style.display = 'flex';
}

function handleEntitySelectChangeModal(select) {
    const modal = select.closest('#edit-modal');
    const nameInput = modal.querySelector('.entity-name-modal');
    if (nameInput && !nameInput.value.trim()) {
        nameInput.value = findFriendlyName(select.value);
    }
}

function toggleSensorItemModeModal(checkbox) {
    const modal = checkbox.closest('#edit-modal');
    const entityContainer = modal.querySelector('.sensor-entity-container-modal');
    const templateContainer = modal.querySelector('.sensor-template-container-modal');
    if (checkbox.checked) {
        entityContainer.style.display = 'none';
        templateContainer.style.display = 'block';
    } else {
        entityContainer.style.display = 'block';
        templateContainer.style.display = 'none';
    }
}

function closeEditModal() {
    const modal = document.getElementById('edit-modal');
    if (modal) modal.style.display = 'none';
    _currentEditTr = null;
    _currentEditType = null;
}

function saveEditModal() {
    if (!_currentEditTr || !_currentEditType) return;
    
    const modal = document.getElementById('edit-modal');
    if (!modal) return;
    
    if (_currentEditType === 'entity') {
        const select = modal.querySelector('.entity-eid-modal');
        const entityId = select.value;
        const name = modal.querySelector('.entity-name-modal').value.trim();
        const note = modal.querySelector('.entity-note-modal').value.trim();
        
        _currentEditTr.dataset.entityId = entityId;
        _currentEditTr.dataset.name = name;
        _currentEditTr.dataset.note = note;
        
        _currentEditTr.querySelector('td:nth-child(1) .view-mode-element').textContent = entityId || '(未選択)';
        _currentEditTr.querySelector('td:nth-child(2) .view-mode-element').textContent = name;
        _currentEditTr.querySelector('td:nth-child(3) .view-mode-element').textContent = note;
        
    } else if (_currentEditType === 'camera') {
        const selectEl = modal.querySelector('.camera-source-select-modal');
        const selectVal = selectEl ? selectEl.value : '';
        const source = (selectVal && selectVal !== '__custom__')
            ? selectVal
            : modal.querySelector('.camera-source-modal').value.trim();
        const room = modal.querySelector('.camera-room-modal').value.trim();
        const entity = modal.querySelector('.camera-entity-modal').value.trim();
        const label = modal.querySelector('.camera-label-modal').value.trim();
        const note = modal.querySelector('.camera-note-modal').value.trim();
        const original = getOriginalRowData(_currentEditTr);
        const merged = cleanRowData(mergeRowData(_currentEditTr, {
            source,
            room,
            entity,
            label,
            note,
        }));

        setOriginalRowData(_currentEditTr, merged);
        _currentEditTr.dataset.room = room;
        _currentEditTr.dataset.source = source;
        _currentEditTr.dataset.entity = entity;
        _currentEditTr.dataset.label = label;
        _currentEditTr.dataset.note = note;

        _currentEditTr.querySelector('td:nth-child(1)').textContent = room || '（未設定）';
        _currentEditTr.querySelector('td:nth-child(2)').textContent = source || '（未設定）';
        _currentEditTr.querySelector('td:nth-child(3)').textContent = label || '（未設定）';

    } else if (_currentEditType === 'mic') {
        const source = modal.querySelector('.mic-source-modal').value.trim();
        const room = modal.querySelector('.mic-room-modal').value.trim();
        const label = modal.querySelector('.mic-label-modal').value.trim();
        const entity = modal.querySelector('.mic-entity-modal').value.trim();
        const note = modal.querySelector('.mic-note-modal').value.trim();
        const merged = cleanMicRowData(mergeRowData(_currentEditTr, {
            source,
            room,
            entity,
            label,
            note,
        }));

        setOriginalRowData(_currentEditTr, merged);
        _currentEditTr.dataset.source = source;
        _currentEditTr.dataset.room = room;
        _currentEditTr.dataset.entity = entity;
        _currentEditTr.dataset.label = label;
        _currentEditTr.dataset.note = note;
        _currentEditTr.querySelector('td:nth-child(1)').textContent = room || '（未設定）';
        _currentEditTr.querySelector('td:nth-child(2)').textContent = source || '（未設定）';
        _currentEditTr.querySelector('td:nth-child(3)').textContent = label || '（未設定）';

    } else if (_currentEditType === 'video-media' || _currentEditType === 'audio-media') {
        const id = modal.querySelector('.media-id-modal').value.trim();
        const source = modal.querySelector('.media-source-modal').value.trim();
        const room = modal.querySelector('.media-room-modal').value.trim();
        const label = modal.querySelector('.media-label-modal').value.trim();
        const note = modal.querySelector('.media-note-modal').value.trim();
        const original = getOriginalRowData(_currentEditTr);
        const merged = cleanRowData(mergeRowData(_currentEditTr, {
            id,
            source,
            room,
            label,
            note,
        }));

        setOriginalRowData(_currentEditTr, merged);
        _currentEditTr.dataset.id = id;
        _currentEditTr.dataset.source = source;
        _currentEditTr.dataset.room = room;
        _currentEditTr.dataset.label = label;
        _currentEditTr.dataset.note = note;

        _currentEditTr.querySelector('td:nth-child(1)').textContent = id || '（未設定）';
        _currentEditTr.querySelector('td:nth-child(2)').textContent = source || '（未設定）';
        _currentEditTr.querySelector('td:nth-child(3)').textContent = room || '';
        _currentEditTr.querySelector('td:nth-child(4)').textContent = label || '';

    } else if (_currentEditType === 'speaker') {
        const room = modal.querySelector('.speaker-room-modal').value.trim();
        const label = modal.querySelector('.speaker-label-modal').value.trim();
        const note = modal.querySelector('.speaker-note-modal').value.trim();
        const entity = modal.querySelector('.speaker-ha-entity-modal').value;
        const merged = cleanRowData(mergeRowData(_currentEditTr, {
            room,
            label,
            entity,
            note
        }));

        setOriginalRowData(_currentEditTr, merged);
        _currentEditTr.dataset.room = room;
        _currentEditTr.dataset.label = label;
        _currentEditTr.dataset.entity = entity;
        _currentEditTr.dataset.note = note;

        _currentEditTr.querySelector('td:nth-child(1)').textContent = room || '（未設定）';
        _currentEditTr.querySelector('td:nth-child(2)').textContent = entity || '（未設定）';
        _currentEditTr.querySelector('td:nth-child(3)').textContent = label;
        
    } else if (_currentEditType === 'sensor') {
        const label = modal.querySelector('.sensor-label-modal').value.trim();
        const isTemplate = modal.querySelector('.sensor-is-template-modal').checked;
        const entity = modal.querySelector('.sensor-entity-modal').value;
        const template = modal.querySelector('.sensor-template-modal').value.trim();
        const note = modal.querySelector('.sensor-note-modal').value.trim();
        
        _currentEditTr.dataset.label = label;
        _currentEditTr.dataset.isTemplate = isTemplate ? 'true' : 'false';
        _currentEditTr.dataset.entity = entity;
        _currentEditTr.dataset.template = template;
        _currentEditTr.dataset.note = note;
        
        const valueDisplay = isTemplate ? template : entity;
        
        _currentEditTr.querySelector('td:nth-child(1) .view-mode-element').textContent = label;
        _currentEditTr.querySelector('td:nth-child(2) .view-mode-element').textContent = valueDisplay || '(未選択)';
        _currentEditTr.querySelector('td:nth-child(3) .view-mode-element').textContent = note;
        
    } else if (_currentEditType === 'projection') {
        const slug = modal.querySelector('.pt-slug-modal').value.trim();
        const id = slug ? `external://${slug}` : '';
        const displayName = modal.querySelector('.pt-name-modal').value.trim();
        const roomSelect = modal.querySelector('.pt-room-modal');
        
        let roomText = '指定なし';
        let roomValue = '';
        if (roomSelect) {
            if (roomSelect.tagName.toLowerCase() === 'select') {
                const selectedOpt = roomSelect.options[roomSelect.selectedIndex];
                roomText = selectedOpt ? (selectedOpt.value ? selectedOpt.textContent : '指定なし') : '指定なし';
                roomValue = selectedOpt ? selectedOpt.value : '';
            } else {
                roomText = roomSelect.value || '指定なし';
                roomValue = roomSelect.value || '';
            }
        }
        
        _currentEditTr.dataset.id = id;
        _currentEditTr.dataset.displayName = displayName;
        _currentEditTr.dataset.room = roomValue;
        
        _currentEditTr.querySelector('td:nth-child(1) .view-mode-element').textContent = id || '(新規デバイス)';
        _currentEditTr.querySelector('td:nth-child(2) .view-mode-element').textContent = displayName;
        
        const roomDisplayEl = _currentEditTr.querySelector('.room-display');
        if (roomDisplayEl) {
            roomDisplayEl.textContent = roomText;
        }
    }
    
    isSettingsDirty = true;
    closeEditModal();
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
    const isLoop = group.contexts?.includes('loop');
    const isChat = group.contexts?.includes('chat');

    card.innerHTML = `
        <div class="sensor-group-header">
            <input type="text" class="sensor-group-title sensor-group-title-input form-input" placeholder="グループ名 (例: 人感センサー)" value="${esc(title)}">
            
            <div class="checkbox-group">
                <span>コンテキスト:</span>
                <label class="checkbox-label">
                    <input type="checkbox" class="sensor-context-loop" ${isLoop ? 'checked' : ''}> loop (自律ループ)
                </label>
                <label class="checkbox-label">
                    <input type="checkbox" class="sensor-context-chat" ${isChat ? 'checked' : ''}> chat (会話)
                </label>
            </div>

            <button type="button" class="btn btn-secondary btn-sm" onclick="addSensorItemRow(this)">＋ 項目追加</button>
            <button type="button" class="btn-remove" onclick="this.closest('.sensor-group-card').remove()">✕ グループ削除</button>
        </div>
        
        <div class="table-responsive">
            <table class="settings-table">
                <thead>
                    <tr>
                        <th>ラベル (label)</th>
                        <th>エンティティ or テンプレート</th>
                        <th>メモ (note)</th>
                        <th style="width: 50px; text-align: center;">編集</th>
                        <th style="width: 50px; text-align: center;">削除</th>
                    </tr>
                </thead>
                <tbody class="sensor-items-list">
                    <!-- Dynamic rows -->
                </tbody>
            </table>
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

function renderSensorItemRow(container, item = { label: '', entity: '', template: '', note: '' }, isNew = false) {
    const tr = document.createElement('tr');
    tr.className = 'sensor-item-row';

    // Set dataset attributes
    tr.dataset.label = item.label || '';
    tr.dataset.entity = item.entity || '';
    tr.dataset.template = item.template || '';
    tr.dataset.note = item.note || '';
    tr.dataset.isTemplate = item.template ? 'true' : 'false';

    const initialValDisplay = item.template ? (item.template || '') : (item.entity || '');

    tr.innerHTML = `
        <td>
            <div class="view-mode-element">${esc(item.label || '')}</div>
        </td>
        <td>
            <div class="view-mode-element font-mono">${esc(initialValDisplay || '(未選択)')}</div>
        </td>
        <td>
            <div class="view-mode-element">${esc(item.note || '')}</div>
        </td>
        <td style="text-align: center; vertical-align: middle;">
            <button type="button" class="btn-edit" onclick="openEditModal('sensor', this.closest('tr'))" title="編集">✏️</button>
        </td>
        <td style="text-align: center; vertical-align: middle;">
            <button type="button" class="btn-remove-icon" onclick="if(confirm('このセンサーを削除しますか？')) { this.closest('.sensor-item-row').remove(); isSettingsDirty = true; }" title="削除">✕</button>
        </td>
    `;

    container.appendChild(tr);

    if (isNew) {
        openEditModal('sensor', tr);
    }
    return tr;
}

function addSensorItemRow(btn) {
    const card = btn.closest('.sensor-group-card');
    const container = card.querySelector('.sensor-items-list');
    renderSensorItemRow(container, { label: '', entity: '', template: '', note: '' }, true);
}

function addSensorGroup() {
    createSensorGroupCard();
}

function highlightAndFocusValidationError(element, message) {
    showSaveStatus(message, 'error');
    if (!element) return;

    // 祖先タブコンテンツを探す
    const tabContent = element.closest('.settings-tab-content');
    if (tabContent && tabContent.id) {
        const tabName = tabContent.id.replace('settings-tab-', '');
        switchSettingsTab(tabName);
    }

    // 一時的に赤いボーダーを付ける
    element.style.outline = '2px solid red';
    element.focus();
    element.scrollIntoView({ behavior: 'smooth', block: 'center' });

    // input/changeイベントでボーダーを消す
    const removeOutline = () => {
        element.style.outline = '';
        element.removeEventListener('input', removeOutline);
        element.removeEventListener('change', removeOutline);
    };
    element.addEventListener('input', removeOutline);
    element.addEventListener('change', removeOutline);
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
    const newHomePolicy = document.getElementById('setting-home-policy')?.value || "";
    
    if (!newCharacter || newCharacter.trim().length < 10) {
        highlightAndFocusValidationError(document.getElementById('setting-character'), 'キャラクター定義が短すぎるか空です。保存を中断しました。');
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
        let validationError = null;
        let errorInput = null;
        document.querySelectorAll('#speakers-tbody .speaker-item').forEach(tr => {
            if (!tr.dataset.room && !validationError) {
                validationError = "スピーカー設定で部屋名が空の項目があります。";
                errorInput = tr;
            }
        });
        if (validationError) {
            highlightAndFocusValidationError(errorInput, validationError);
            return;
        }
        nextPrefs = serializeFormToPrefs();
    }

    let jsonEditorNeedsUpdate = false;

    // ループ設定は専用フォームの値を常に適用する（JSON直接編集タブとは独立）
    const loopIntervalMin = parseFloat(document.getElementById('setting-loop-interval-min')?.value);
    if (!isNaN(loopIntervalMin) && loopIntervalMin > 0) {
        nextPrefs.loop_schedule = {
            loop_interval: Math.round(loopIntervalMin * 60),
            day_probability: parseInt(document.getElementById('setting-loop-day-prob')?.value, 10) || 0,
            late_probability: parseInt(document.getElementById('setting-loop-late-prob')?.value, 10) || 0,
            night_probability: parseInt(document.getElementById('setting-loop-night-prob')?.value, 10) || 0,
            min_probability: parseInt(document.getElementById('setting-loop-min-prob')?.value, 10) || 0,
        };
        jsonEditorNeedsUpdate = true;
    }

    // HTTP POST送信設定も専用トグルの値を常に適用する
    const httpPostEnabledEl = document.getElementById('http-post-enabled-toggle');
    if (httpPostEnabledEl) {
        nextPrefs.http_post_enabled = httpPostEnabledEl.checked;
        jsonEditorNeedsUpdate = true;
    }

    // カメラ静止画履歴設定も専用フォームの値を適用する
    const cameraHistoryEnabledEl = document.getElementById('setting-camera-history-enabled');
    const cameraHistoryMinutesEl = document.getElementById('setting-camera-history-minutes');
    if (cameraHistoryEnabledEl) {
        nextPrefs.camera_history_enabled = cameraHistoryEnabledEl.checked;
        jsonEditorNeedsUpdate = true;
    }
    if (cameraHistoryMinutesEl) {
        const parsed = parseInt(cameraHistoryMinutesEl.value, 10);
        nextPrefs.camera_history_minutes = isNaN(parsed) ? 10 : Math.min(60, Math.max(1, parsed));
        jsonEditorNeedsUpdate = true;
    }

    if (jsonEditorNeedsUpdate && activeSettingsTab === 'advanced' && jsonEditor) {
        jsonEditor.setValue(JSON.stringify(nextPrefs, null, 2));
    }

    const spk = nextPrefs.speakers;
    const hasSpeakers = Array.isArray(spk) ? spk.length > 0 : Object.keys(spk || {}).length > 0;
    if (!hasSpeakers) {
        const addSpeakerBtn = document.querySelector('button[onclick*="addSpeakerRow"]');
        highlightAndFocusValidationError(addSpeakerBtn, 'スピーカーが1つも登録されていません。');
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
        const [prefsRes, charRes, extraContextRes, homePolicyRes] = await Promise.all([
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
            }),
            fetch(`${base}/api/home-policy`, {
                method: 'PUT',
                headers: { 'Content-Type': 'text/plain; charset=utf-8' },
                body: newHomePolicy
            })
        ]);

        if (!prefsRes.ok || !charRes.ok || !extraContextRes.ok || !homePolicyRes.ok) {
            const pErr = !prefsRes.ok ? (await prefsRes.json()).error : null;
            const cErr = !charRes.ok ? (await charRes.json()).error : null;
            const eErr = !extraContextRes.ok ? (await extraContextRes.json()).error : null;
            const hErr = !homePolicyRes.ok ? (await homePolicyRes.json()).error : null;
            throw new Error(pErr || cErr || eErr || hErr || "保存に失敗しました。");
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
                statusEl.style.color = "var(--color-success)";
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
                statusEl.style.color = "var(--color-success)";
                setTimeout(() => { statusEl.textContent = ""; }, 4000);
            }
        } else {
            const data = await response.json();
            const errMsg = data.error || "失敗";
            if (statusEl) {
                statusEl.textContent = `✗ 失敗: ${errMsg}`;
                statusEl.style.color = "var(--color-danger-hover)";
                setTimeout(() => { statusEl.textContent = ""; }, 6000);
            }
        }
    } catch (err) {
        btn.disabled = false;
        if (statusEl) {
            statusEl.textContent = `✗ エラー: ${err.message}`;
            statusEl.style.color = "var(--color-danger-hover)";
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
                roomInfo = `${characterName}: ${bodyRoom} / 音源: ${sourceRoom}`;
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
    } finally {
        btn.disabled = false;
    }
}

// ==========================================================================
// AI Lounge & Feature Catalog Implementation
// ==========================================================================

function initMockPreferences() {
    if (!prefsData) {
        prefsData = {
            enabled_features: ["ai_lounge", "non_speech_audio"],
            camera_history_enabled: false,
            camera_history_minutes: 10,
            ai_lounge: {
                auto_approve: false
            },
            character_name: "エージェント",
            cameras: [],
            mics: [],
            video_media: [],
            audio_media: [],
            speakers: {},
            entities: [],
            presence: { entity: "" },
            policies: [],
            sensors: { groups: [] },
            loop_schedule: { loop_interval: 1800, day_probability: 100, late_probability: 30, night_probability: 10, min_probability: 0 }
        };
        updateCharacterName(prefsData);
        updateDynamicFeaturesUI();
    }
}

function updateDynamicFeaturesUI() {
    const enabled = prefsData?.enabled_features || [];
    
    // non_speech_audio -> 耳にした音(room-audio)
    const audioRoom = document.getElementById('room-audio');
    if (audioRoom) {
        audioRoom.style.display = enabled.includes('non_speech_audio') ? 'flex' : 'none';
    }
    
    // ai_lounge -> AI Lounge nav item
    const loungeRoom = document.getElementById('room-lounge');
    if (loungeRoom) {
        const isLoungeEnabled = enabled.includes('ai_lounge');
        loungeRoom.style.display = isLoungeEnabled ? 'flex' : 'none';
        if (isLoungeEnabled) {
            startAiLoungeLoop();
        } else {
            stopAiLoungeLoop();
            if (activeRoom === 'lounge') switchRoom('chat');
        }
    }
    
    // 自動承認トグルの同期
    const autoApproveToggle = document.getElementById('ai-lounge-auto-approve-toggle');
    if (autoApproveToggle) {
        autoApproveToggle.checked = !!(prefsData?.ai_lounge?.auto_approve);
    }
}

function startAiLoungeLoop() {
    if (aiLoungeTimer) return;
    fetchAiLoungeData();
    aiLoungeTimer = setInterval(fetchAiLoungeData, 30000);
}

function stopAiLoungeLoop() {
    if (aiLoungeTimer) {
        clearInterval(aiLoungeTimer);
        aiLoungeTimer = null;
    }
}

function populateLoungeCredentials() {
    const appIdEl = document.getElementById('lounge-app-id-input');
    const installIdEl = document.getElementById('lounge-installation-id-input');
    if (appIdEl) appIdEl.value = prefsData?.ai_lounge?.app_id || '';
    if (installIdEl) installIdEl.value = prefsData?.ai_lounge?.installation_id || '';
}

async function saveLoungeCredentials() {
    const appId = (document.getElementById('lounge-app-id-input')?.value || '').trim();
    const installId = (document.getElementById('lounge-installation-id-input')?.value || '').trim();
    const msgEl = document.getElementById('lounge-credentials-msg');
    if (!prefsData) return;
    if (!prefsData.ai_lounge) prefsData.ai_lounge = {};
    prefsData.ai_lounge.app_id = appId;
    prefsData.ai_lounge.installation_id = installId;
    if (isStandaloneMode) {
        if (msgEl) msgEl.textContent = '保存しました（モック）';
        return;
    }
    try {
        const res = await fetch(`${base}/api/preferences`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(prefsData)
        });
        if (res.ok) {
            if (msgEl) { msgEl.textContent = '保存しました'; msgEl.style.color = ''; }
            setTimeout(() => { if (msgEl) msgEl.textContent = ''; }, 2000);
        } else {
            if (msgEl) { msgEl.textContent = '保存失敗'; msgEl.style.color = 'var(--color-danger)'; }
        }
    } catch (e) {
        if (msgEl) { msgEl.textContent = `エラー: ${e.message}`; msgEl.style.color = 'var(--color-danger)'; }
    }
}

async function fetchLoungePemStatus() {
    if (isStandaloneMode) return;
    try {
        const res = await fetch(`${base}/api/lounge-pem-status`);
        if (!res.ok) return;
        const data = await res.json();
        const setupEl = document.getElementById('lounge-pem-setup');
        const okEl = document.getElementById('lounge-pem-ok');
        if (data.exists) {
            if (setupEl) setupEl.style.display = 'none';
            if (okEl) okEl.style.display = 'block';
        } else {
            if (setupEl) setupEl.style.display = 'block';
            if (okEl) okEl.style.display = 'none';
        }
    } catch (e) { /* ignore */ }
}

let _pendingPemContent = null;

function handlePemFileSelect(input) {
    const file = input.files[0];
    if (!file) return;
    const filenameEl = document.getElementById('lounge-pem-filename');
    const uploadBtn = document.getElementById('lounge-pem-upload-btn');
    const statusEl = document.getElementById('lounge-pem-status-msg');
    if (filenameEl) filenameEl.textContent = file.name;
    if (statusEl) statusEl.textContent = '';
    const reader = new FileReader();
    reader.onload = e => {
        _pendingPemContent = e.target.result;
        if (uploadBtn) uploadBtn.disabled = false;
    };
    reader.readAsText(file);
}

async function uploadPemFile() {
    if (!_pendingPemContent) return;
    const uploadBtn = document.getElementById('lounge-pem-upload-btn');
    const statusEl = document.getElementById('lounge-pem-status-msg');
    if (uploadBtn) uploadBtn.disabled = true;
    if (statusEl) statusEl.textContent = 'アップロード中...';
    try {
        const res = await fetch(`${base}/api/lounge-pem`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pem: _pendingPemContent })
        });
        const data = await res.json();
        if (res.ok && data.ok) {
            if (statusEl) statusEl.textContent = '';
            _pendingPemContent = null;
            const filenameEl = document.getElementById('lounge-pem-filename');
            if (filenameEl) filenameEl.textContent = 'ファイル未選択';
            const input = document.getElementById('lounge-pem-input');
            if (input) input.value = '';
            fetchLoungePemStatus();
        } else {
            if (statusEl) { statusEl.textContent = `エラー: ${data.error || '不明なエラー'}`; statusEl.style.color = 'var(--color-danger)'; }
            if (uploadBtn) uploadBtn.disabled = false;
        }
    } catch (e) {
        if (statusEl) { statusEl.textContent = `通信エラー: ${e.message}`; statusEl.style.color = 'var(--color-danger)'; }
        if (uploadBtn) uploadBtn.disabled = false;
    }
}

async function fetchAiLoungeData() {
    if (isStandaloneMode) {
        renderAiLoungeQueue(mockLoungeQueue);
        renderAiLoungeLog(mockLoungeLog);
        return;
    }
    fetchLoungePemStatus();
    populateLoungeCredentials();
    try {
        const [queueRes, logRes] = await Promise.all([
            fetch(`${base}/api/lounge-queue`).catch(() => null),
            fetch(`${base}/api/lounge-log`).catch(() => null)
        ]);
        
        let queueData = [];
        if (queueRes && queueRes.ok) {
            queueData = await queueRes.json();
        }
        
        let logData = [];
        if (logRes && logRes.ok) {
            logData = await logRes.json();
        }
        
        renderAiLoungeQueue(queueData);
        renderAiLoungeLog(logData);
    } catch (err) {
        console.warn("Failed to fetch AI Lounge data", err);
    }
}

function renderAiLoungeQueue(queue) {
    const queueList = document.getElementById('ai-lounge-queue-list');
    const queueCount = document.getElementById('ai-lounge-queue-count');
    if (!queueList) return;
    
    if (queueCount) queueCount.textContent = queue.length;
    const loungePreviewEl = document.getElementById('lounge-preview');
    if (loungePreviewEl) loungePreviewEl.textContent = `承認待ち ${queue.length}件`;
    const loungeUnreadEl = document.getElementById('lounge-unread');
    if (loungeUnreadEl) {
        loungeUnreadEl.style.display = queue.length > 0 ? 'flex' : 'none';
        loungeUnreadEl.textContent = queue.length;
    }
    queueList.innerHTML = '';

    if (queue.length === 0) {
        queueList.innerHTML = '<div class="ai-lounge-empty">承認待ちはありません</div>';
        return;
    }
    
    queue.forEach(item => {
        const card = document.createElement('div');
        card.className = 'ai-lounge-card';
        card.id = `lounge-queue-${item.id}`;

        const itemId = String(item.id ?? '');
        const escapedItemId = escapeHtml(itemId);
        let replyHtml = '';
        if (item.reply_to_url) {
            const rawReplyUrl = String(item.reply_to_url);
            let linkText = rawReplyUrl;
            let safeReplyUrl = '';
            try {
                const url = new URL(rawReplyUrl);
                if (url.protocol === 'http:' || url.protocol === 'https:') {
                    safeReplyUrl = url.href;
                    const pathParts = url.pathname.split('/');
                    const lastPath = pathParts[pathParts.length - 2] + '/' + pathParts[pathParts.length - 1];
                    linkText = lastPath + url.hash;
                }
            } catch (e) {
                const parts = rawReplyUrl.split('/');
                linkText = parts[parts.length - 1] || rawReplyUrl;
            }

            const replyLinkHtml = safeReplyUrl
                ? `<a href="${escapeHtml(safeReplyUrl)}" target="_blank" class="ai-lounge-link">🔗 ${escapeHtml(linkText)}</a>`
                : `<span class="ai-lounge-link">🔗 ${escapeHtml(linkText)}</span>`;
            
            replyHtml = `
                <div class="ai-lounge-reply-to">
                    返信先: ${replyLinkHtml}
                </div>
            `;
            
            if (item.reply_to_preview) {
                replyHtml += `
                    <blockquote class="ai-lounge-quote">&gt; "${escapeHtml(item.reply_to_preview)}"</blockquote>
                `;
            }
        }
        
        const typeLabel = item.type === 'new_discussion' ? '新規Discussion:' : '返信:';
        const titleHtml = item.title ? `<div class="ai-lounge-title-preview">タイトル: 「${escapeHtml(item.title)}」</div>` : '';
        const bodyText = escapeHtml(item.body || item.text || '');
        const escapedCharacterName = escapeHtml(characterName);
        card.innerHTML = `
            ${replyHtml}
            <div class="ai-lounge-author">${escapedCharacterName}の${typeLabel}</div>
            ${titleHtml}
            <div class="ai-lounge-text">「${bodyText}」</div>
            <div class="ai-lounge-card-actions" id="actions-${escapedItemId}">
                <button type="button" class="btn btn-primary btn-sm ai-lounge-approve-btn">✓ 承認</button>
                <button type="button" class="btn btn-sm ai-lounge-show-reject-btn" style="color: #dc2626; background: none; border: 1px solid var(--claude-border);">✗ 拒否</button>
            </div>
            <div class="ai-lounge-reject-input-group" id="reject-group-${escapedItemId}" style="display: none;">
                <textarea class="form-input reject-reason-textarea" id="reject-reason-${escapedItemId}" placeholder="拒否理由（任意）" rows="2"></textarea>
                <div class="ai-lounge-card-actions" style="margin-top: 8px;">
                    <button type="button" class="btn btn-danger btn-sm ai-lounge-reject-btn">送信</button>
                    <button type="button" class="btn btn-secondary btn-sm ai-lounge-hide-reject-btn">キャンセル</button>
                </div>
            </div>
        `;

        const approveBtn = card.querySelector('.ai-lounge-approve-btn');
        const showRejectBtn = card.querySelector('.ai-lounge-show-reject-btn');
        const rejectBtn = card.querySelector('.ai-lounge-reject-btn');
        const hideRejectBtn = card.querySelector('.ai-lounge-hide-reject-btn');
        if (approveBtn) approveBtn.addEventListener('click', () => approveLoungeQueue(itemId));
        if (showRejectBtn) showRejectBtn.addEventListener('click', () => showRejectInput(itemId));
        if (rejectBtn) rejectBtn.addEventListener('click', () => rejectLoungeQueue(itemId));
        if (hideRejectBtn) hideRejectBtn.addEventListener('click', () => hideRejectInput(itemId));
        
        queueList.appendChild(card);
    });
}

function renderAiLoungeLog(log) {
    const logList = document.getElementById('ai-lounge-log-list');
    if (!logList) return;
    
    logList.innerHTML = '';
    const recentLogs = log.slice(0, 5);
    
    if (recentLogs.length === 0) {
        logList.innerHTML = '<li class="ai-lounge-log-empty">ログはありません</li>';
        return;
    }
    
    recentLogs.forEach(item => {
        const li = document.createElement('li');
        li.className = 'ai-lounge-log-item';
        
        const dateStr = formatDateMinimal(item.resolved_at || item.timestamp || item.created_at);
        const statusClass = item.status === 'approved' ? 'status-approved' : 'status-rejected';
        const statusIcon = item.status === 'approved' ? '✓' : '✗';
        const statusText = item.status === 'approved' ? '承認 → posted' : '拒否';
        
        let reasonHtml = '';
        if (item.status === 'rejected' && (item.rejection_reason || item.reason)) {
            reasonHtml = ` — ${escapeHtml(item.rejection_reason || item.reason)}`;
        }
        
        li.innerHTML = `
            <span class="ai-lounge-log-status ${statusClass}">${statusIcon}</span>
            <span class="ai-lounge-log-time">${dateStr}</span>
            <span class="ai-lounge-log-desc">${statusText}${reasonHtml}</span>
        `;
        
        logList.appendChild(li);
    });
}

function formatDateMinimal(isoString) {
    try {
        const date = new Date(isoString);
        const y = date.getFullYear();
        const m = String(date.getMonth() + 1).padStart(2, '0');
        const d = String(date.getDate()).padStart(2, '0');
        const hh = String(date.getHours()).padStart(2, '0');
        const mm = String(date.getMinutes()).padStart(2, '0');
        return `${y}-${m}-${d} ${hh}:${mm}`;
    } catch (e) {
        return "";
    }
}

function showRejectInput(id) {
    const actions = document.getElementById(`actions-${id}`);
    const rejectGroup = document.getElementById(`reject-group-${id}`);
    if (actions && rejectGroup) {
        actions.style.display = 'none';
        rejectGroup.style.display = 'block';
    }
}

function hideRejectInput(id) {
    const actions = document.getElementById(`actions-${id}`);
    const rejectGroup = document.getElementById(`reject-group-${id}`);
    if (actions && rejectGroup) {
        actions.style.display = 'flex';
        rejectGroup.style.display = 'none';
    }
}

async function approveLoungeQueue(id) {
    if (isStandaloneMode) {
        console.log(`[Mock] Approved queue item ${id}`);
        const item = mockLoungeQueue.find(q => q.id === id);
        if (item) {
            mockLoungeLog.unshift({
                id: "l_new_" + Date.now(),
                timestamp: new Date().toISOString(),
                status: "approved",
                text: item.body || item.text
            });
            mockLoungeQueue = mockLoungeQueue.filter(q => q.id !== id);
        }
        animateRemoveCard(id);
        return;
    }
    
    try {
        const response = await fetch(`${base}/api/lounge-queue/${id}/approve`, {
            method: 'POST'
        });
        if (response.ok) {
            animateRemoveCard(id);
        } else {
            console.error("Failed to approve lounge queue item");
        }
    } catch (err) {
        console.warn("Error approving queue item", err);
    }
}

async function rejectLoungeQueue(id) {
    const reasonTextarea = document.getElementById(`reject-reason-${id}`);
    const reason = reasonTextarea ? reasonTextarea.value.trim() : "";
    
    if (isStandaloneMode) {
        console.log(`[Mock] Rejected queue item ${id} with reason: ${reason}`);
        const item = mockLoungeQueue.find(q => q.id === id);
        if (item) {
            mockLoungeLog.unshift({
                id: "l_new_" + Date.now(),
                timestamp: new Date().toISOString(),
                status: "rejected",
                reason: reason || undefined,
                text: item.body || item.text
            });
            mockLoungeQueue = mockLoungeQueue.filter(q => q.id !== id);
        }
        animateRemoveCard(id);
        return;
    }
    
    try {
        const response = await fetch(`${base}/api/lounge-queue/${id}/reject`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason: reason })
        });
        if (response.ok) {
            animateRemoveCard(id);
        } else {
            console.error("Failed to reject lounge queue item");
        }
    } catch (err) {
        console.warn("Error rejecting queue item", err);
    }
}

function animateRemoveCard(id) {
    const card = document.getElementById(`lounge-queue-${id}`);
    if (card) {
        card.style.transition = 'all 0.3s ease';
        card.style.opacity = '0';
        card.style.maxHeight = '0';
        card.style.paddingTop = '0';
        card.style.paddingBottom = '0';
        card.style.marginTop = '0';
        card.style.marginBottom = '0';
        card.style.border = 'none';
        
        setTimeout(() => {
            fetchAiLoungeData();
        }, 300);
    }
}

async function handleToggleAutoApprove(checkbox) {
    const autoApprove = checkbox.checked;
    if (!prefsData.ai_lounge) {
        prefsData.ai_lounge = {};
    }
    prefsData.ai_lounge.auto_approve = autoApprove;
    
    if (isStandaloneMode) {
        console.log(`[Mock] Toggled auto_approve: ${autoApprove}`);
        return;
    }
    
    try {
        await fetch(`${base}/api/preferences`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(prefsData)
        });
    } catch (err) {
        console.warn("Failed to patch preferences for auto_approve", err);
    }
}

async function handleToggleHttpPostEnabled(checkbox) {
    const httpPostEnabled = checkbox.checked;
    const oldVal = !!prefsData.http_post_enabled;
    prefsData.http_post_enabled = httpPostEnabled;
    
    if (isStandaloneMode) {
        console.log(`[Mock] Toggled http_post_enabled: ${httpPostEnabled}`);
        return;
    }
    
    try {
        const response = await fetch(`${base}/api/preferences`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(prefsData)
        });
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
    } catch (err) {
        console.warn("Failed to patch preferences for http_post_enabled", err);
        checkbox.checked = oldVal;
        prefsData.http_post_enabled = oldVal;
    }
}

function handleCameraHistoryToggle(checkbox) {
    const minutesEl = document.getElementById('setting-camera-history-minutes');
    if (minutesEl) {
        minutesEl.disabled = !checkbox.checked;
    }
    isSettingsDirty = true;
}

function renderOtherFeaturesCatalog() {
    const container = document.getElementById('settings-tab-other');
    if (!container) return;
    
    if (!voicevoxSongStatusLoaded) {
        voicevoxSongStatusLoaded = true;
        loadVoicevoxSongStatus().then(() => {
            renderOtherFeaturesCatalog();
            if (voicevoxSongStatus.status === 'running') {
                startVoicevoxSongPolling();
            }
        });
    }
    
    const enabled = prefsData?.enabled_features || [];

    let html = `
        <section class="settings-section card">
            <h3>その他の機能（機能カタログ）</h3>
            <p class="section-desc">追加のアドオン機能を有効化できます。有効化すると左サイドバーにセクションが表示されます。</p>
            <div class="feature-catalog-list">
    `;

    FEATURE_CATALOG.forEach(feature => {
        const isEnabled = enabled.includes(feature.id);
        let btnHtml = '';

        if (feature.disabled) {
            btnHtml = `<button type="button" class="btn btn-secondary btn-sm" disabled>準備中</button>`;
        } else if (isEnabled) {
            btnHtml = `<button type="button" class="btn btn-secondary btn-sm" disabled style="background-color: var(--claude-border); color: var(--claude-text-sub);">追加済み ✓</button>`;
        } else {
            btnHtml = `<button type="button" class="btn btn-primary btn-sm" onclick="addFeature('${feature.id}')">追加</button>`;
        }

        html += `
            <div class="feature-catalog-card">
                <div class="feature-catalog-icon">${feature.icon}</div>
                <div class="feature-catalog-info">
                    <div class="feature-catalog-name">${feature.name}</div>
                    <div class="feature-catalog-desc">${(feature.descriptionTemplate || feature.description || '').replace('{name}', characterName)}</div>
                </div>
                <div class="feature-catalog-action">
                    ${btnHtml}
                </div>
            </div>
        `;
    });

    // VOICEVOX Song は重いインストール処理を伴うため専用の状態管理だが、
    // 見た目は他のカタログカードと同じ .feature-catalog-card に統一する
    const isInstalled = voicevoxSongStatus.installed;
    const status = voicevoxSongStatus.status;
    const message = voicevoxSongStatus.message;

    let voicevoxBtn = '';
    let voicevoxDesc = `${characterName}（または選択したキャラクター）の声で歌を歌えるようにする機能です。1.7GBのモデルファイルのダウンロードが必要です。`;

    if (status === 'running') {
        voicevoxBtn = `<button type="button" class="btn btn-secondary btn-sm" disabled>インストール中...</button>`;
        voicevoxDesc += ` <span style="color: var(--claude-accent); font-weight: 500;">進捗: ${message || ''}</span>`;
    } else if (isInstalled) {
        voicevoxBtn = `<button type="button" class="btn btn-secondary btn-sm" onclick="uninstallVoicevoxSong()" style="color:#dc2626;">アンインストール</button>`;
    } else {
        voicevoxBtn = `<button type="button" class="btn btn-primary btn-sm" onclick="installVoicevoxSong()">インストール</button>`;
        if (status === 'error' && message) {
            voicevoxDesc += ` <span style="color: #dc2626; font-weight: 500;">エラー: ${message}</span>`;
        }
    }

    html += `
            <div class="feature-catalog-card">
                <div class="feature-catalog-icon">🎤</div>
                <div class="feature-catalog-info">
                    <div class="feature-catalog-name">VOICEVOX Song（歌唱合成）</div>
                    <div class="feature-catalog-desc">${voicevoxDesc}</div>
                </div>
                <div class="feature-catalog-action">
                    ${voicevoxBtn}
                </div>
            </div>
    `;

    html += `
            </div>
        </section>
    `;

    container.innerHTML = html;
}

async function addFeature(featureId) {
    if (!prefsData) return;
    if (!prefsData.enabled_features) {
        prefsData.enabled_features = [];
    }
    if (!prefsData.enabled_features.includes(featureId)) {
        prefsData.enabled_features.push(featureId);
    }
    
    if (isStandaloneMode) {
        console.log(`[Mock] Added feature ${featureId}`);
        updateDynamicFeaturesUI();
        renderOtherFeaturesCatalog();
    } else {
        try {
            const response = await fetch(`${base}/api/preferences`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(prefsData)
            });
            if (response.ok) {
                updateDynamicFeaturesUI();
                renderOtherFeaturesCatalog();
            } else {
                console.error("Failed to update preferences with new feature");
            }
        } catch (err) {
            console.error("Error updating preferences:", err);
        }
    }
}

async function loadVoicevoxSongStatus() {
    if (isStandaloneMode) {
        voicevoxSongStatus = { ...mockVoicevoxSongStatus };
        if (voicevoxSongStatus.installed) {
            voicevoxSongSingers = [...mockVoicevoxSongSingers];
        } else {
            voicevoxSongSingers = [];
        }
        return;
    }
    try {
        const res = await fetch(`${base}/api/voicevox_song/status`);
        if (res.ok) {
            voicevoxSongStatus = await res.json();
            if (voicevoxSongStatus.installed) {
                await loadVoicevoxSongSingers();
            } else {
                voicevoxSongSingers = [];
            }
        }
    } catch (e) {
        console.error("Failed to load VOICEVOX Song status:", e);
    }
}

async function loadVoicevoxSongSingers() {
    if (isStandaloneMode) {
        voicevoxSongSingers = [...mockVoicevoxSongSingers];
        return;
    }
    try {
        const res = await fetch(`${base}/api/voicevox_song/singers`);
        if (res.ok) {
            voicevoxSongSingers = await res.json();
        }
    } catch (e) {
        console.error("Failed to load VOICEVOX Song singers:", e);
        voicevoxSongSingers = [];
    }
}

function startVoicevoxSongPolling() {
    if (voicevoxSongPollInterval) return;
    
    voicevoxSongPollInterval = setInterval(async () => {
        if (isStandaloneMode) {
            if (mockVoicevoxSongStatus.status === 'running') {
                if (!mockVoicevoxSongStatus.progress) mockVoicevoxSongStatus.progress = 0;
                mockVoicevoxSongStatus.progress += 20;
                mockVoicevoxSongStatus.message = `ダウンロード中... ${mockVoicevoxSongStatus.progress}% (1.7GB)`;
                
                if (mockVoicevoxSongStatus.progress >= 100) {
                    mockVoicevoxSongStatus.status = 'done';
                    mockVoicevoxSongStatus.installed = true;
                    mockVoicevoxSongStatus.message = 'インストール完了';
                    voicevoxSongStatus = { ...mockVoicevoxSongStatus };
                    clearInterval(voicevoxSongPollInterval);
                    voicevoxSongPollInterval = null;
                    await loadVoicevoxSongSingers();
                    renderOtherFeaturesCatalog();
                    updateSingSpeakerUI();
                    return;
                }
                voicevoxSongStatus = { ...mockVoicevoxSongStatus };
                renderOtherFeaturesCatalog();
            }
            return;
        }
        
        try {
            const res = await fetch(`${base}/api/voicevox_song/status`);
            if (res.ok) {
                voicevoxSongStatus = await res.json();
                renderOtherFeaturesCatalog();
                
                if (voicevoxSongStatus.status === 'done' || voicevoxSongStatus.status === 'error') {
                    clearInterval(voicevoxSongPollInterval);
                    voicevoxSongPollInterval = null;
                    if (voicevoxSongStatus.status === 'done') {
                        await loadVoicevoxSongSingers();
                        renderOtherFeaturesCatalog();
                        updateSingSpeakerUI();
                    } else if (voicevoxSongStatus.status === 'error') {
                        alert('VOICEVOX Song インストール失敗: ' + (voicevoxSongStatus.message || '不明なエラー'));
                    }
                }
            }
        } catch (e) {
            console.error("Polling error:", e);
            clearInterval(voicevoxSongPollInterval);
            voicevoxSongPollInterval = null;
        }
    }, 2000);
}

async function installVoicevoxSong() {
    if (isStandaloneMode) {
        mockVoicevoxSongStatus.status = 'running';
        mockVoicevoxSongStatus.progress = 0;
        mockVoicevoxSongStatus.message = 'ダウンロード開始中...';
        voicevoxSongStatus = { ...mockVoicevoxSongStatus };
        renderOtherFeaturesCatalog();
        startVoicevoxSongPolling();
        return;
    }
    
    voicevoxSongStatus.status = 'running';
    voicevoxSongStatus.message = 'インストールを開始しています...';
    renderOtherFeaturesCatalog();
    
    try {
        const res = await fetch(`${base}/api/voicevox_song/install`, { method: 'POST' });
        if (res.ok) {
            startVoicevoxSongPolling();
        } else {
            alert('インストール開始に失敗しました');
            await loadVoicevoxSongStatus();
            renderOtherFeaturesCatalog();
        }
    } catch (e) {
        alert('インストール開始に失敗しました');
        await loadVoicevoxSongStatus();
        renderOtherFeaturesCatalog();
    }
}

async function uninstallVoicevoxSong() {
    if (!confirm('VOICEVOX Song モデルを削除してアンインストールします。よろしいですか？')) return;
    
    if (isStandaloneMode) {
        mockVoicevoxSongStatus.installed = false;
        mockVoicevoxSongStatus.status = 'idle';
        mockVoicevoxSongStatus.message = '';
        voicevoxSongStatus = { ...mockVoicevoxSongStatus };
        voicevoxSongSingers = [];
        if (prefsData) {
            delete prefsData.sing_speaker;
        }
        renderOtherFeaturesCatalog();
        updateSingSpeakerUI();
        return;
    }
    
    try {
        const res = await fetch(`${base}/api/voicevox_song/uninstall`, { method: 'POST' });
        if (res.ok) {
            await loadVoicevoxSongStatus();
            if (prefsData) {
                delete prefsData.sing_speaker;
                try {
                    await fetch(`${base}/api/preferences`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(prefsData)
                    });
                } catch (err) {
                    console.error("Failed to update preferences after uninstall:", err);
                }
            }
            renderOtherFeaturesCatalog();
            updateSingSpeakerUI();
        } else {
            const data = await res.json();
            alert('アンインストール失敗: ' + (data.error || '不明なエラー'));
        }
    } catch (e) {
        alert('通信エラーが発生しました');
    }
}

function updateSingSpeakerUI() {
    const accordion = document.getElementById('accordion-sing-speaker');
    const select = document.getElementById('setting-sing-speaker');
    if (!accordion || !select) return;

    const isInstalled = voicevoxSongStatus && (voicevoxSongStatus.installed || voicevoxSongStatus.status === 'done');
    if (isInstalled) {
        accordion.style.display = 'block';
        
        const currentSinger = prefsData?.sing_speaker || {};
        const optionsHtml = ['<option value="">(未選択)</option>'];
        voicevoxSongSingers.forEach(s => {
            const isSelected = currentSinger.style_id === s.style_id ? 'selected' : '';
            optionsHtml.push(`<option value="${s.style_id}" ${isSelected}>${s.name} (${s.style_name}) [${s.credit}]</option>`);
        });
        select.innerHTML = optionsHtml.join('');
    } else {
        accordion.style.display = 'none';
    }
}
