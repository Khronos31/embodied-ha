# ベースは python slim（増分5b）。Claude Code同梱廃止(5a)でnode実行時依存が無くなったため
# node:22-slim から乗り換え、イメージを削減する。python は現行本番(Debian bookworm=3.11)と
# 同minorの 3.11 を維持し回帰面を最小化(3.12以降への更新は別TODO)。Debian世代は bookworm 据え置き
# なので apt 依存(ffmpeg/libasound2-plugins 等)は不変。
FROM python:3.11-slim-bookworm

# システム依存パッケージ（python3 / pip はベースイメージ同梱のため apt からは外す）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates jq git mosquitto-clients \
    libasound2-plugins pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --break-system-packages --no-cache-dir faster-whisper --quiet 2>/dev/null || true
RUN python3 -m pip install --break-system-packages --no-cache-dir "pysilero-vad==3.2.0" \
    && python3 -c "from pysilero_vad import SileroVoiceActivityDetector; SileroVoiceActivityDetector()"

# Claude CLI は同梱しない（増分5a）。Web UI から DIY 配置（/data/claude-cli/bin/claude、
# claude_setup.install() 経由）する。run.sh が CLAUDE_BIN/EHA_CLAUDE_BIN を配置先へ配線する。

# スクリプト一式をコンテナに含める
COPY . /app/

# 実行権限付与 + コマンド名でそのまま呼べるようシンボリックリンク
RUN chmod +x /app/run.sh /app/loop.sh /app/chat.sh \
             /app/recall.sh /app/loops.sh \
    && ln -s /app/loops.sh   /usr/local/bin/loops \
    && ln -s /app/recall.sh  /usr/local/bin/recall

# CLAUDE_BIN は同梱前提の直呼び（=claude）を廃止（増分5a）。run.sh が DIY 配置先を
# 解決して CLAUDE_BIN/EHA_CLAUDE_BIN へ配線するため、ここでは固定しない
# （固定すると run.sh/invoke-agent.sh の DIY フォールバックを shadow してしまう）。
ENV EHA_TOOLS_PATH=/usr/local/bin

CMD ["bash", "/app/run.sh"]
