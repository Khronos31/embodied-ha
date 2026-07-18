#!/usr/bin/env bash
# Re-runnable agent-setup smoke test. Requires network access to GitHub/OpenAI
# and a Linux environment with bash, curl, and Python 3. It downloads Codex to
# a temporary directory; it neither uses nor changes the add-on's /data state.
#
# This verifies: Codex DIY install/version, device-auth SSE URL+code emission
# and disconnect cleanup, plus the old/new setup and Claude status shapes.
# It deliberately does not approve the browser login. To complete it manually,
# open the displayed URL, enter the displayed code, then wait for SSE "done".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$SCRIPT_DIR/embodied_ha"
WORK_DIR="$(mktemp -d -t eha-agent-setup.XXXXXX)"
SERVER_PID=""
CURL_PID=""
CODEX_PID=""

cleanup() {
    if [ -n "$CURL_PID" ]; then kill "$CURL_PID" 2>/dev/null || true; fi
    if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; fi
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM

export EHA_CODEX_INSTALL_ROOT="$WORK_DIR/codex-cli"
export EHA_CODEX_HOME="$WORK_DIR/codex-home"
export CODEX_HOME="$EHA_CODEX_HOME"
export HA_URL="http://homeassistant.invalid"
export EHA_DATA_DIR="$WORK_DIR/data"
export EHA_LOG_DIR="$WORK_DIR/log"
export EHA_ANTIGRAVITY_HOME="$WORK_DIR/antigravity-home"
export CLAUDE_CONFIG_DIR="$WORK_DIR/claude-home"
export EHA_SETUP_GUARD="off"  # local smoke intentionally calls loopback
mkdir -p "$EHA_CODEX_HOME" "$EHA_DATA_DIR" "$EHA_LOG_DIR" \
    "$EHA_ANTIGRAVITY_HOME" "$CLAUDE_CONFIG_DIR"

echo '[smoke] install Codex via the DIY release downloader'
PYTHONPATH="$APP_DIR" python3 -c 'import codex_setup; codex_setup.install(progress=print)'
"$EHA_CODEX_INSTALL_ROOT/bin/codex" --version

PORT="$(python3 -c 'import socket; sock=socket.socket(); sock.bind(("127.0.0.1", 0)); print(sock.getsockname()[1]); sock.close()')"
export INGRESS_PORT="$PORT"
python3 "$APP_DIR/web/server.py" >"$WORK_DIR/server.log" 2>&1 &
SERVER_PID=$!
BASE_URL="http://127.0.0.1:$PORT"
for _ in $(seq 1 50); do
    if curl --fail --silent "$BASE_URL/api/setup/codex/status" >/dev/null; then break; fi
    sleep 0.1
done
curl --fail --silent "$BASE_URL/api/setup/codex/status" | python3 -m json.tool
curl --fail --silent "$BASE_URL/api/setup/status" | python3 -m json.tool
curl --fail --silent "$BASE_URL/api/setup/claude/status" | python3 -m json.tool

echo '[smoke] start device-auth SSE; browser approval is intentionally skipped'
SSE_FILE="$WORK_DIR/login.sse"
curl --request POST --no-buffer --silent "$BASE_URL/api/setup/codex/login" >"$SSE_FILE" &
CURL_PID=$!
for _ in $(seq 1 100); do
    if grep -Eq 'https://auth\.openai\.com/codex/device' "$SSE_FILE" && grep -Eq '[A-Z0-9]{4}-[A-Z0-9]{5}' "$SSE_FILE"; then
        break
    fi
    sleep 0.1
done
grep -E 'https://auth\.openai\.com/codex/device|[A-Z0-9]{4}-[A-Z0-9]{5}' "$SSE_FILE"
CODEX_PID="$(pgrep -f "$EHA_CODEX_INSTALL_ROOT/bin/codex login --device-auth" | head -n 1 || true)"
test -n "$CODEX_PID"
kill "$CURL_PID" 2>/dev/null || true
wait "$CURL_PID" 2>/dev/null || true
CURL_PID=""
for _ in $(seq 1 50); do
    if ! kill -0 "$CODEX_PID" 2>/dev/null; then break; fi
    sleep 0.1
done
if kill -0 "$CODEX_PID" 2>/dev/null; then
    echo "[smoke] device-auth child still running: $CODEX_PID" >&2
    exit 1
fi
echo '[smoke] OK'
