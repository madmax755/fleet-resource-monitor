#!/bin/sh
# Docker HEALTHCHECK: fail if heartbeat is missing or older than 2 check intervals.
set -eu

HEARTBEAT_FILE="${HEARTBEAT_FILE:-/data/heartbeat}"
CHECK_INTERVAL_SECONDS="${CHECK_INTERVAL_SECONDS:-900}"
# Allow two missed loops plus a small buffer before declaring the checker dead.
MAX_AGE_SECONDS=$((CHECK_INTERVAL_SECONDS * 2 + 120))

if [ ! -f "$HEARTBEAT_FILE" ]; then
  echo "heartbeat missing: $HEARTBEAT_FILE"
  exit 1
fi

now="$(date +%s)"
# heartbeat stores a float unix timestamp on the first line
ts="$(awk 'NR==1 {print int($1)}' "$HEARTBEAT_FILE")"
if [ -z "${ts:-}" ]; then
  echo "heartbeat unreadable"
  exit 1
fi

age=$((now - ts))
if [ "$age" -gt "$MAX_AGE_SECONDS" ]; then
  echo "heartbeat stale: age=${age}s max=${MAX_AGE_SECONDS}s"
  exit 1
fi

echo "heartbeat ok age=${age}s"
exit 0
