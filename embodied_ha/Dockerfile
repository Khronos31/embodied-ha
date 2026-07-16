FROM node:22-slim

# システム依存パッケージ
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip ffmpeg curl ca-certificates jq git mosquitto-clients \
    libasound2-plugins pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/*

# Debianのmedia-typesパッケージは/etc/mime.typesに.wavをaudio/x-wavとしてのみ
# 登録しており、audio/wavが無い。Antigravity(agy.real)のバイナリは文字列
# "audio/wav"を保持しているが"audio/x-wav"は保持しておらず(strings確認済み)、
# queued listen等のWAVファイル解析が"unsupported mime type audio/wave"で
# 失敗する原因になっていた。パッケージインストール後に不足分を冪等に追記する。
#
# 実機検証(2026-07-16): audio/wav追記後、agy -pへ.wavを@<path>形式で渡すと
# --dangerously-skip-permissions無し・スクリプト実行禁止でも直接読み込み
# (view_file)で正確な音声内容を取得できることを確認した。
#
# 注意: .mp3は対象外。agy.realバイナリは文字列"audio/mp3"を一切保持しておらず
# (strings確認済み)、既存の"audio/mpeg"マッピングでも実機テストでMP3の直接
# 読み込みはタイムアウトし機能しなかった。production側のqueued listenは
# audio_daemon.pyのwrite_wav()で常にWAVのみを生成するため、MP3対応は
# 現時点で不要と判断し追加しない。
RUN grep -qxF 'audio/wav					wav' /etc/mime.types 2>/dev/null \
      || echo 'audio/wav					wav' >> /etc/mime.types

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
