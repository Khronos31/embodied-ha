#!/bin/sh
# Independent resident-hearing recovery guard for the disposable F-46 comparator.
set -u

if [ "$#" -ne 4 ]; then
  echo "usage: $0 CANARY_SLUG RESIDENT_SLUG MAX_CAPTURE_SECONDS RECEIPT_FILE" >&2
  exit 2
fi

canary_slug="$1"
resident_slug="$2"
max_capture_seconds="$3"
receipt_file="$4"
ha_bin="${HA_BIN:-ha}"
curl_bin="${CURL_BIN:-curl}"
start_grace_seconds="${START_GRACE_SECONDS:-30}"
restore_timeout_seconds="${RESTORE_TIMEOUT_SECONDS:-120}"
poll_seconds="${POLL_SECONDS:-2}"
restore_not_before_epoch="${RESTORE_NOT_BEFORE_EPOCH:-0}"

case "$max_capture_seconds" in
  *[!0-9]*|'') echo "MAX_CAPTURE_SECONDS must be an integer" >&2; exit 2 ;;
esac
case "$restore_not_before_epoch" in
  *[!0-9]*|'') echo "RESTORE_NOT_BEFORE_EPOCH must be an integer" >&2; exit 2 ;;
esac

mkdir -p "$(dirname "$receipt_file")"
exec >>"$receipt_file" 2>&1

timestamp() {
  date -Iseconds
}

addon_field() {
  "$ha_bin" addons info "$1" 2>/dev/null | awk -v field="$2" '
    $1 == field ":" { print $2; exit }
  '
}

marker_count() {
  count=$("$ha_bin" addons logs "$canary_slug" 2>/dev/null \
    | awk '/^F46_MATCHED_CAPTURE_COMPLETE / { found += 1 } END { print found + 0 }')
  echo "$count"
}

echo "$(timestamp) WATCHDOG_STARTED"
initial_markers=$(marker_count)
started_deadline=$(( $(date +%s) + start_grace_seconds ))
while :; do
  current_markers=$(marker_count)
  if [ "$current_markers" -gt "$initial_markers" ]; then
    trigger="capture_complete"
    break
  fi
  canary_state=$(addon_field "$canary_slug" state)
  if [ "$canary_state" = "started" ]; then
    break
  fi
  if [ "$(date +%s)" -ge "$started_deadline" ]; then
    trigger="canary_never_started"
    break
  fi
  sleep "$poll_seconds"
done

if [ "${trigger:-}" = "" ]; then
  capture_deadline=$(( $(date +%s) + max_capture_seconds ))
  while :; do
    current_markers=$(marker_count)
    if [ "$current_markers" -gt "$initial_markers" ]; then
      trigger="capture_complete"
      break
    fi
    canary_state=$(addon_field "$canary_slug" state)
    if [ "$canary_state" != "started" ]; then
      trigger="canary_stopped_before_capture"
      break
    fi
    if [ "$(date +%s)" -ge "$capture_deadline" ]; then
      trigger="capture_timeout"
      "$ha_bin" addons stop "$canary_slug" || true
      break
    fi
    sleep "$poll_seconds"
  done
fi

echo "$(timestamp) RESTORE_TRIGGER reason=$trigger"
if [ "$(date +%s)" -lt "$restore_not_before_epoch" ]; then
  echo "$(timestamp) RESTORE_DEFERRED until_epoch=$restore_not_before_epoch"
fi
while [ "$(date +%s)" -lt "$restore_not_before_epoch" ]; do
  sleep "$poll_seconds"
done
"$ha_bin" addons start "$resident_slug" || true
restore_deadline=$(( $(date +%s) + restore_timeout_seconds ))
while [ "$(date +%s)" -lt "$restore_deadline" ]; do
  resident_state=$(addon_field "$resident_slug" state)
  resident_ip=$(addon_field "$resident_slug" ip_address)
  web_status=000
  if [ -n "$resident_ip" ]; then
    web_status=$("$curl_bin" -sS -o /dev/null -w '%{http_code}' \
      "http://$resident_ip:8099/" 2>/dev/null || echo 000)
  fi
  resident_logs=$("$ha_bin" addons logs "$resident_slug" 2>/dev/null || true)
  stream_count=$(printf '%s\n' "$resident_logs" \
    | awk '/tcp pull state=streaming label=/ {
        line=$0
        sub(/^.*label=/, "", line)
        sub(/ generation=.*$/, "", line)
        seen[line]=1
      }
      END { for (label in seen) count += 1; print count + 0 }')
  if [ "$resident_state" = "started" ] \
    && [ "$web_status" = "200" ] \
    && [ "$stream_count" -ge 5 ]; then
    echo "$(timestamp) RESTORE_PASS state=$resident_state web=$web_status tcp_sources=$stream_count"
    exit 0
  fi
  sleep "$poll_seconds"
done

echo "$(timestamp) RESTORE_FAIL state=${resident_state:-unknown} web=${web_status:-000} tcp_sources=${stream_count:-0}"
exit 1
