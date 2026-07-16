FROM node:22-slim

# システム依存パッケージ
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip ffmpeg curl ca-certificates jq git mosquitto-clients \
    libasound2-plugins pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*

# Debianのmedia-typesパッケージは/etc/mime.typesにwav/mp3を
# audio/x-wav・audio/mpegとしてのみ登録しており、audio/wav・audio/mp3が無い。
# Antigravity(agy) CLIのファイル読み込みがこの片方しか受け付けず、
# queued listen等の音声ファイル解析が失敗する原因になるため、
# パッケージインストール後に不足分を追記する(冪等)。
RUN grep -qxF 'audio/wav					wav' /etc/mime.types 2>/dev/null \
      || echo 'audio/wav					wav' >> /etc/mime.types \
    && grep -qxF 'audio/mp3					mp3' /etc/mime.types 2>/dev/null \
      || echo 'audio/mp3					mp3' >> /etc/mime.types

RUN python3 -m pip install --break-system-packages --no-cache-dir faster-whisper --quiet 2>/dev/null || true
RUN python3 -m pip install --break-system-packages --no-cache-dir "pysilero-vad==3.2.0" \
    && python3 -c "from pysilero_vad import SileroVoiceActivityDetector; SileroVoiceActivityDetector()"

# Claude CLI（node:22-slim に npm が入っているのでそのまま使う）
RUN npm install -g @anthropic-ai/claude-code

# スクリプト一式をコンテナに含める
COPY . /app/

# 実行権限付与 + コマンド名でそのまま呼べるようシンボリックリンク
RUN chmod +x /app/run.sh /app/loop.sh /app/chat.sh \
             /app/recall.sh /app/loops.sh \
    && ln -s /app/loops.sh   /usr/local/bin/loops \
    && ln -s /app/recall.sh  /usr/local/bin/recall

# コンテナ内ではシステムの claude を使う。
# CLAUDE_CONFIG_DIR は /data に設定するので run.sh 側のデフォルトを上書き。
ENV CLAUDE_BIN=claude
ENV EHA_TOOLS_PATH=/usr/local/bin

CMD ["bash", "/app/run.sh"]
