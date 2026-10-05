#!/usr/bin/env bash
# Stop the benchmark etcd (graceful, then forced) and make sure nothing is left on the
# client port. Safe to call when nothing is running.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

if [[ $MODE == vm ]]; then
  if systemctl is-active --quiet etcd-bench 2>/dev/null; then
    timeout 120 systemctl stop etcd-bench || systemctl kill -s KILL etcd-bench || true
  fi
  systemctl reset-failed etcd-bench 2>/dev/null || true
fi

pidfile="$STATE_DIR/etcd.pid"
if [[ -f $pidfile ]]; then
  pid=$(cat "$pidfile")
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 60); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
fi

# Belt and braces: any etcd still bound to our member name.
pkill -9 -f -- "--name=$ETCD_NAME " 2>/dev/null || true
for _ in $(seq 30); do
  curl -fsS --max-time 1 "$(client_endpoint)/health" >/dev/null 2>&1 || exit 0
  sleep 1
done
die "something is still answering on $(client_endpoint)"
