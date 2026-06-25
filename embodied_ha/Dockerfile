FROM node:22-slim

# システム依存パッケージ
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip ffmpeg curl ca-certificates jq git mosquitto-clients \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --break-system-packages --no-cache-dir faster-whisper --quiet 2>/dev/null || true

# Claude CLI（node:22-slim に npm が入っているのでそのまま使う）
RUN npm install -g @anthropic-ai/claude-code

# スクリプト一式をコンテナに含める
COPY . /app/

# 実行権限付与 + コマンド名でそのまま呼べるようシンボリックリンク
RUN chmod +x /app/run.sh /app/watch.sh /app/explore.sh /app/chat.sh \
             /app/recall.sh /app/loops.sh \
    && ln -s /app/loops.sh   /usr/local/bin/loops \
    && ln -s /app/recall.sh  /usr/local/bin/recall

# コンテナ内ではシステムの claude を使う。
# CLAUDE_CONFIG_DIR は /data に設定するので run.sh 側のデフォルトを上書き。
ENV CLAUDE_BIN=claude
ENV EHA_TOOLS_PATH=/usr/local/bin

CMD ["bash", "/app/run.sh"]
