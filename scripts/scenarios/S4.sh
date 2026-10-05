# shellcheck shell=bash disable=SC2034,SC2153
# S4: etcd's own tools/rw-heatmaps/rw-benchmark.sh, unmodified, once per variant.
# The script starts and kills its own etcd for every cell, so run-one.sh hands control
# to scenario_client and skips its own start/collector steps.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
SCENARIO_GOLDEN=none
SCENARIO_CAPPED=0
SCENARIO_MGLRU=on
SCENARIO_SELF_MANAGED=1

# Sweep settings for rw-benchmark.sh (SPEC section 7 reduced matrix; tiny in smoke mode).
s4_export() {
  # rw-benchmark.sh reads CLIENT_PORT from the environment; pin it to the script's own
  # default so its etcd can never collide with ours.
  export CLIENT_PORT=23790
  if [[ -n ${SMOKE:-} ]]; then
    export RATIO_LIST="1" VALUE_SIZE_POWER_RANGE="8 8" CONN_CLI_COUNT_POWER_RANGE="5 6" REPEAT_COUNT=1 RUN_COUNT=2000
  else
    # Value sizes 2^8, 2^10, 2^12 (rw-benchmark.sh passes the range to `seq`, so "8 2 12"
    # means 8 10 12): 5 ratios x 3 sizes x 4 connection counts = 60 cells, ~4.6 min each.
    # Reduced from 100 cells with the user on 2026-10-03 so S4 finishes with the others.
    export RATIO_LIST="1/8 1/2 1 2 8" VALUE_SIZE_POWER_RANGE="8 2 12" CONN_CLI_COUNT_POWER_RANGE="5 8" REPEAT_COUNT=5 RUN_COUNT=200000
  fi
}

# Variant guard for S4: the first etcd the script starts must carry (control) or lack
# (treatment) the rr flag on its db mapping. The script runs etcd as
# "<etcd>/tools/rw-heatmaps/../../bin/etcd --quota-backend-bytes=...", hence the pattern.
s4_guard() {
  local variant=$1 deadline=$((SECONDS + 180)) pid flags
  while ((SECONDS < deadline)); do
    pid=$(pgrep -f -- "/bin/etcd --quota-backend-bytes" | head -1 || true)
    if [[ -n $pid ]] && flags=$(python3 "$HERE/vmflags.py" "$pid" member/snap/db 2>/dev/null); then
      python3 "$HERE/runjson.py" set "$RUNDIR/run.json" "db_vmflags=$flags"
      if [[ $variant == control && " $flags " != *" rr "* ]] || [[ $variant == treatment && " $flags " == *" rr "* ]]; then
        return 20
      fi
      return 0
    fi
    sleep 1
  done
  return 1
}

scenario_client() {
  local rundir=$1 variant=$2 bin=$3 rc=0 script_pid
  [[ -d $ETCD_SRC/tools/rw-heatmaps ]] || { SCENARIO_FAIL_REASON="no etcd tree at $ETCD_SRC"; return 20; }
  mkdir -p "$ETCD_SRC/bin/tools" "$DATA_MOUNT/rwbench"
  install -m 0755 "$bin" "$ETCD_SRC/bin/etcd"
  install -m 0755 "$BENCHMARK" "$ETCD_SRC/bin/tools/benchmark"
  [[ $(sha256_of "$ETCD_SRC/bin/etcd") == "$(sha256_of "$bin")" ]] || { SCENARIO_FAIL_REASON="bin/etcd copy mismatch"; return 20; }
  (
    s4_export
    printf 'RATIO_LIST="%s" VALUE_SIZE_POWER_RANGE="%s" CONN_CLI_COUNT_POWER_RANGE="%s" REPEAT_COUNT=%s RUN_COUNT=%s tools/rw-heatmaps/rw-benchmark.sh -w %s\n' \
      "$RATIO_LIST" "$VALUE_SIZE_POWER_RANGE" "$CONN_CLI_COUNT_POWER_RANGE" "$REPEAT_COUNT" "$RUN_COUNT" "$DATA_MOUNT/rwbench" >>"$rundir/commands.txt"
  )
  mark sweep-start
  (s4_export && cd "$rundir" && bash "$ETCD_SRC/tools/rw-heatmaps/rw-benchmark.sh" -w "$DATA_MOUNT/rwbench") \
    >"$rundir/rw-benchmark.log" 2>&1 &
  script_pid=$!
  if [[ $MODE != mac ]]; then
    s4_guard "$variant" || rc=$?
    if ((rc != 0)); then
      kill "$script_pid" 2>/dev/null || true
      pkill -9 -f -- "/bin/etcd --quota-backend-bytes" 2>/dev/null || true
      wait "$script_pid" 2>/dev/null || true
      if ((rc == 20)); then SCENARIO_FAIL_REASON="variant guard: $variant has wrong db VmFlags"; else SCENARIO_FAIL_REASON="could not read etcd VmFlags"; fi
      return "$rc"
    fi
  fi
  wait "$script_pid" || { SCENARIO_FAIL_REASON="rw-benchmark.sh exited non-zero"; return 1; }
  mark sweep-end
  pkill -9 -f -- "/bin/etcd --quota-backend-bytes" 2>/dev/null || true
  local csv
  csv=$(find "$rundir" -maxdepth 1 -name "result-*.csv" | head -1)
  [[ -n $csv ]] || { SCENARIO_FAIL_REASON="no result CSV"; return 1; }
  if ! python3 "$HERE/validate_s4.py" "$csv" >"$rundir/csv-check.txt" 2>&1; then
    SCENARIO_FAIL_REASON="rw-benchmark CSV has empty or zero cells"
    return 11
  fi
}
