#!/usr/bin/env bash
# Supplementary S4 drift check. S4 runs the whole control sweep, then
# the whole treatment sweep hours later (~7 h here), so a uniform shift between them could be drift
# on the VM. After the S4 matrix, this reruns the first 8 cells of the sweep (ratio 1/8,
# values 256 B and 1 KiB, all 4 connection counts) with control, so control vs control
# across the whole gap bounds the drift. Same unmodified rw-benchmark.sh and guards as S4.
#
# Usage: s4-recheck.sh OUTDIR     as root on E, after bench-matrix has finished
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/lib.sh"
load_runtime_env
out=$1
log_x() { echo "$(date -u +%FT%TZ) s4-recheck $*" | tee -a "$STATE_DIR/extra.log"; }
fail() { log_x "-> failed: $1"; exit 1; }

[[ ! -e $out/csv-check.txt ]] || { log_x "already done ($out)"; exit 0; }
mkdir -p "$out" "$ETCD_SRC/bin/tools" "$DATA_MOUNT/rwbench"
bin="$VARIANTS_ROOT/control/etcd"
[[ $(sha256_of "$bin") == "$(expected_sha control etcd)" ]] || fail "control etcd sha mismatch"
install -m 0755 "$bin" "$ETCD_SRC/bin/etcd"
install -m 0755 "$VARIANTS_ROOT/control/benchmark" "$ETCD_SRC/bin/tools/benchmark"
[[ $(sha256_of "$ETCD_SRC/bin/etcd") == "$(sha256_of "$bin")" ]] || fail "bin/etcd copy mismatch"

export CLIENT_PORT=23790 RATIO_LIST="1/8" VALUE_SIZE_POWER_RANGE="8 2 10" \
  CONN_CLI_COUNT_POWER_RANGE="5 8" REPEAT_COUNT=5 RUN_COUNT=200000
env | grep -E '^(RATIO_LIST|VALUE_SIZE_POWER_RANGE|CONN_CLI_COUNT_POWER_RANGE|REPEAT_COUNT|RUN_COUNT|CLIENT_PORT)=' \
  >"$out/commands.txt"
log_x "start (control, 8 cells)"
date -u +%s >"$out/start.ts"
(cd "$out" && bash "$ETCD_SRC/tools/rw-heatmaps/rw-benchmark.sh" -w "$DATA_MOUNT/rwbench") \
  >"$out/rw-benchmark.log" 2>&1 &
script_pid=$!

# Variant guard: the first etcd the script starts must carry the rr flag (control).
deadline=$((SECONDS + 180)) flags=""
while ((SECONDS < deadline)); do
  pid=$(pgrep -f -- "/bin/etcd --quota-backend-bytes" | head -1 || true)
  if [[ -n $pid ]] && flags=$(python3 "$HERE/vmflags.py" "$pid" member/snap/db 2>/dev/null); then break; fi
  flags=""
  sleep 1
done
echo "$flags" >"$out/db_vmflags.txt"
if [[ " $flags " != *" rr "* ]]; then
  kill "$script_pid" 2>/dev/null || true
  pkill -9 -f -- "/bin/etcd --quota-backend-bytes" 2>/dev/null || true
  fail "variant guard: control db VmFlags '$flags' lack rr"
fi

wait "$script_pid" || fail "rw-benchmark.sh exited non-zero"
date -u +%s >"$out/end.ts"
pkill -9 -f -- "/bin/etcd --quota-backend-bytes" 2>/dev/null || true
csv=$(find "$out" -maxdepth 1 -name "result-*.csv" | sort | tail -1)
[[ -n $csv ]] || fail "no result CSV"
python3 "$HERE/validate_s4.py" "$csv" >"$out/csv-check.txt" 2>&1 || fail "CSV has empty or zero cells"
log_x "finished ok ($csv)"
