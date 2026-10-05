#!/bin/bash
# Readahead sensitivity arm (supplementary, added after the 25% cap S2 results; additive,
# no existing data touched). S2 at the 25% cap with read_ahead_kb=128 (kernel
# default) instead of the tuned profile's 4096, to isolate how much of treatment's read
# regression at the 25% cap on C comes from the 4 MiB readaround. Control ignores
# readahead (MADV_RANDOM), so only treatment should move. Runs on D as root.
set -euo pipefail
ra=/sys/block/nvme1n1/queue/read_ahead_kb
out=/opt/bench/results/_s2-cap25-ra128
mkdir -p "$out"
log() { echo "$(date -u +%FT%TZ) ra128 $*" >>/opt/bench/state/extra.log; }
echo 128 >"$ra"
[[ $(cat "$ra") == 128 ]] || { log "-> failed: could not set read_ahead_kb"; exit 20; }
echo "$(date -u +%FT%TZ) before read_ahead_kb=$(cat "$ra")" >>"$out/readahead.txt"
log "start S2 at 25% cap, read_ahead_kb=128"
set +e
RESULTS_ROOT=$out PAIRS_S2=3 CAP_OVERRIDE=2442752000 MATRIX_LOG=/opt/bench/state/matrix.log \
  /opt/bench/scripts/run-matrix.sh --scenarios S2
rc=$?
set -e
now=$(cat "$ra")
echo "$(date -u +%FT%TZ) after read_ahead_kb=$now rc=$rc" >>"$out/readahead.txt"
[[ $now == 128 ]] || log "-> failed: read_ahead_kb changed during the run ($now)"
echo 4096 >"$ra"
log "finished rc=$rc, read_ahead_kb restored to $(cat "$ra")"
exit "$rc"
