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
required_fresh_streams="${REQUIRED_FRESH_STREAMS:-5}"
protected_hash_file="${PROTECTED_HASH_FILE:-}"
resident_options_sha256="${RESIDENT_OPTIONS_SHA256:-}"
yq_bin="${YQ_BIN:-yq}"
jq_bin="${JQ_BIN:-jq}"
sha256sum_bin="${SHA256SUM_BIN:-sha256sum}"

case "$max_capture_seconds" in
  *[!0-9]*|'') echo "MAX_CAPTURE_SECONDS must be an integer" >&2; exit 2 ;;
esac
case "$restore_not_before_epoch" in
  *[!0-9]*|'') echo "RESTORE_NOT_BEFORE_EPOCH must be an integer" >&2; exit 2 ;;
esac
case "$required_fresh_streams" in
  *[!0-9]*|'') echo "REQUIRED_FRESH_STREAMS must be an integer" >&2; exit 2 ;;
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

stream_counts() {
  awk '/tcp pull state=streaming label=/ {
      line=$0
      sub(/^.*label=/, "", line)
      sub(/ generation=.*$/, "", line)
      generation=$0
      sub(/^.* generation=/, "", generation)
      sub(/ .*$/, "", generation)
      if (generation + 0 > count[line]) count[line] = generation + 0
    }
    END { for (label in count) print label "\t" count[label] }'
}

fresh_stream_count() {
  awk -F '\t' '
    NR == FNR { baseline[$1]=$2; next }
    $2 != baseline[$1] { fresh += 1 }
    END { print fresh + 0 }
  ' "$1" "$2"
}

resident_hashes_match() {
  if [ -n "$resident_options_sha256" ]; then
    actual_options_sha256=$("$ha_bin" addons info "$resident_slug" 2>/dev/null \
      | "$yq_bin" -o=json '.options' 2>/dev/null \
      | "$jq_bin" -S . 2>/dev/null \
      | "$sha256sum_bin" \
      | awk '{print $1}')
    [ "$actual_options_sha256" = "$resident_options_sha256" ] || return 1
  fi
  if [ -n "$protected_hash_file" ]; then
    [ -f "$protected_hash_file" ] || return 1
    "$sha256sum_bin" --status -c "$protected_hash_file" || return 1
  fi
  return 0
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
watchdog_tmp=$(mktemp -d)
trap 'rm -rf "$watchdog_tmp"' EXIT HUP INT TERM
"$ha_bin" addons logs "$resident_slug" 2>/dev/null | stream_counts > "$watchdog_tmp/stream-counts-before"
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
  printf '%s\n' "$resident_logs" | stream_counts > "$watchdog_tmp/stream-counts-current"
  stream_count=$(fresh_stream_count \
    "$watchdog_tmp/stream-counts-before" \
    "$watchdog_tmp/stream-counts-current")
  hashes=fail
  if resident_hashes_match; then
    hashes=pass
  fi
  if [ "$resident_state" = "started" ] \
    && [ "$web_status" = "200" ] \
    && [ "$stream_count" -ge "$required_fresh_streams" ] \
    && [ "$hashes" = "pass" ]; then
    echo "$(timestamp) RESTORE_PASS state=$resident_state web=$web_status fresh_tcp_sources=$stream_count hashes=$hashes"
    exit 0
  fi
  sleep "$poll_seconds"
done

echo "$(timestamp) RESTORE_FAIL state=${resident_state:-unknown} web=${web_status:-000} fresh_tcp_sources=${stream_count:-0} hashes=${hashes:-fail}"
exit 1
