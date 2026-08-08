#!/bin/sh
# External wall-clock guard for native VAD replay in the disposable comparator.
set -u

if [ "$#" -ne 4 ]; then
  echo "usage: $0 CANARY_SLUG MAX_CAPTURE_SECONDS MAX_REPLAY_SECONDS RECEIPT_FILE" >&2
  exit 2
fi

canary_slug="$1"
max_capture_seconds="$2"
max_replay_seconds="$3"
receipt_file="$4"
ha_bin="${HA_BIN:-ha}"
poll_seconds="${POLL_SECONDS:-2}"

for value in "$max_capture_seconds" "$max_replay_seconds"; do
  case "$value" in
    *[!0-9]*|'') echo "timeouts must be integers" >&2; exit 2 ;;
  esac
done

mkdir -p "$(dirname "$receipt_file")"
exec >>"$receipt_file" 2>&1

timestamp() {
  date -Iseconds
}

marker_count() {
  marker="$1"
  "$ha_bin" addons logs "$canary_slug" 2>/dev/null \
    | awk -v marker="$marker" 'index($0, marker) == 1 { found += 1 } END { print found + 0 }'
}

addon_state() {
  "$ha_bin" addons info "$canary_slug" 2>/dev/null \
    | awk '$1 == "state:" { print $2; exit }'
}

initial_capture=$(marker_count "F46_MATCHED_CAPTURE_COMPLETE ")
initial_result=$(marker_count "F46_MATCHED_RESULT ")
capture_deadline=$(( $(date +%s) + max_capture_seconds ))
echo "$(timestamp) REPLAY_GUARD_STARTED"

while :; do
  if [ "$(marker_count "F46_MATCHED_CAPTURE_COMPLETE ")" -gt "$initial_capture" ]; then
    echo "$(timestamp) REPLAY_GUARD_CAPTURED"
    break
  fi
  if [ "$(date +%s)" -ge "$capture_deadline" ]; then
    "$ha_bin" addons stop "$canary_slug" || true
    echo "$(timestamp) REPLAY_GUARD_CAPTURE_TIMEOUT"
    exit 1
  fi
  state=$(addon_state)
  if [ -n "$state" ] && [ "$state" != "started" ] && [ "$state" != "stopped" ]; then
    echo "$(timestamp) REPLAY_GUARD_CANARY_STOPPED state=$state"
    exit 1
  fi
  sleep "$poll_seconds"
done

replay_deadline=$(( $(date +%s) + max_replay_seconds ))
while :; do
  if [ "$(marker_count "F46_MATCHED_RESULT ")" -gt "$initial_result" ]; then
    echo "$(timestamp) REPLAY_GUARD_RESULT"
    exit 0
  fi
  state=$(addon_state)
  if [ "$state" != "started" ]; then
    echo "$(timestamp) REPLAY_GUARD_CANARY_STOPPED state=$state"
    exit 1
  fi
  if [ "$(date +%s)" -ge "$replay_deadline" ]; then
    "$ha_bin" addons stop "$canary_slug" || true
    echo "$(timestamp) REPLAY_GUARD_REPLAY_TIMEOUT"
    exit 1
  fi
  sleep "$poll_seconds"
done
