#!/usr/bin/env bash
# One run of one scenario for one variant (PLAN P6 steps 0-6).
#
# Usage: run-one.sh SCENARIO VARIANT RUNDIR
#
# Exit codes: 0 ok; 10 failed (retryable); 11 invalid (retryable); 20 fatal (stop the
# matrix: wrong binary, bad checksum, MGLRU in the wrong state, missing config).
# The outcome is always recorded in RUNDIR/run.json (status, reason).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/lib.sh"
load_runtime_env

scenario=$1 variant=$2 rundir=$3
[[ $variant == control || $variant == treatment ]] || die "variant must be control or treatment"
[[ -f $HERE/scenarios/$scenario.sh ]] || die "unknown scenario $scenario"
mkdir -p "$rundir"
rundir=$(cd "$rundir" && pwd)
RJ="$rundir/run.json"
ETCDCTL="$VARIANTS_ROOT/control/etcdctl"
BENCHMARK="$VARIANTS_ROOT/control/benchmark"
export RUNDIR="$rundir" ETCDCTL BENCHMARK

# shellcheck source=scenarios/S3a.sh
source "$HERE/scenarios/$scenario.sh"
: "${SCENARIO_GOLDEN:?}" "${SCENARIO_CAPPED:?}"
SCENARIO_SELF_MANAGED=${SCENARIO_SELF_MANAGED:-0}
SCENARIO_MGLRU=${SCENARIO_MGLRU:-on}

collector_pid=""
finish() {
  local status=$1 reason=$2 code=$3
  if [[ -n $collector_pid ]]; then
    kill "$collector_pid" 2>/dev/null || true
    wait "$collector_pid" 2>/dev/null || true
  fi
  collector_pid=""
  if [[ $SCENARIO_SELF_MANAGED != 1 ]]; then
    curl -fsS --max-time 10 "http://127.0.0.1:$METRICS_PORT/metrics" >"$rundir/metrics-end.txt" 2>/dev/null || true
    "$HERE/stop-etcd.sh" || true
  fi
  python3 "$HERE/runjson.py" set "$RJ" "status=$status" "reason=$reason" "end_ts=$(date +%s)"
  log "$scenario/$variant -> $status${reason:+ ($reason)}"
  trap - EXIT
  exit "$code"
}
trap 'finish failed "unexpected exit at line $LINENO" 10' EXIT

tooling_rev=$(cat "$HERE/run-one.sh" "$HERE/lib.sh" "$HERE/collect.py" "$HERE/start-etcd.sh" | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-12)
python3 "$HERE/runjson.py" set "$RJ" \
  "scenario=$scenario" "variant=$variant" "vm=${VM_ROLE:-local}" "mode=$MODE" \
  "smoke=${SMOKE:-}" "fault=${FAULT:-}" "start_ts=$(date +%s)" \
  "scenario_rev=${SCENARIO_REV:-}" "tooling_rev=$tooling_rev" "golden=$SCENARIO_GOLDEN" \
  "hostname=$(uname -n)" "kernel=$(uname -r)"

mark() { python3 -c 'import sys,time; print(f"{time.time():.3f},{sys.argv[1]}")' "$1" >>"$rundir/marks.csv"; }

# ---- 0. Guards ---------------------------------------------------------------------
bin="$VARIANTS_ROOT/$variant/etcd"
fault_is wrong-binary && bin="$VARIANTS_ROOT/control/etcd"
if [[ $MODE != mac ]]; then
  mountpoint -q "$DATA_MOUNT" || finish invalid "$DATA_MOUNT not mounted" 11
  (($(free_gib "$DATA_MOUNT") >= MIN_FREE_DATA_GIB)) || finish invalid "low disk on $DATA_MOUNT" 11
  (($(free_gib /) >= MIN_FREE_ROOT_GIB)) || finish invalid "low disk on /" 11
fi
if [[ $MODE == vm ]]; then
  want_mglru=0x0007
  [[ $SCENARIO_MGLRU == off ]] && want_mglru=0x0000
  have_mglru=$(cat /sys/kernel/mm/lru_gen/enabled)
  [[ $have_mglru == "$want_mglru" ]] || finish fatal "MGLRU is $have_mglru, scenario needs $want_mglru" 20
  python3 "$HERE/runjson.py" set "$RJ" "mglru=$have_mglru"
fi
want_sha=$(expected_sha "$variant" etcd)
# wrong-binary simulates a wrong build that still matches its recorded checksum, so the
# rr variant guard (not the checksum guard) is what has to catch it.
fault_is wrong-binary && want_sha=$(expected_sha control etcd)
fault_is bad-sha && want_sha=deliberately-wrong
have_sha=$(sha256_of "$bin")
[[ -n $want_sha ]] || finish fatal "no expected checksum for $variant (versions.env missing?)" 20
[[ $have_sha == "$want_sha" ]] || finish fatal "binary checksum mismatch for $variant" 20
python3 "$HERE/runjson.py" set "$RJ" "binary_sha=$have_sha"

if [[ $SCENARIO_CAPPED == 1 ]]; then
  cap=${CAP_OVERRIDE:-${CAP_BYTES:-}}
  [[ -n $cap ]] || finish fatal "capped scenario but no CAP_BYTES (cap.env missing?)" 20
  # The kernel stores memory.max in whole pages; use the page-rounded value everywhere.
  cap=$((cap / 4096 * 4096))
else
  cap=infinity
fi
# CAP_AT=healthy (default): start uncapped, apply the cap once etcd is healthy, i.e. a
# running etcd squeezed by memory pressure. CAP_AT=start: capped from the first byte,
# which also makes startup (index rebuild) run under pressure.
CAP_AT=${CAP_AT:-healthy}
start_cap=$cap
[[ $cap != infinity && $CAP_AT == healthy ]] && start_cap=infinity
python3 "$HERE/runjson.py" set "$RJ" "cap=$cap" "cap_at=$CAP_AT"

client() {
  if [[ $MODE != mac && -n ${CLIENT_CPUS:-} ]]; then taskset -c "$CLIENT_CPUS" "$@"; else "$@"; fi
}
# bench_step NAME ARGS...: run the official benchmark, keep its output, fail on errors.
# shellcheck disable=SC2329 # called from scenario files
bench_step() {
  local name=$1; shift
  printf 'benchmark %s\n' "$*" >>"$rundir/commands.txt"
  mark "start:$name"
  if fault_is bench-error-always; then return 1; fi
  if fault_is bench-error; then
    local once="$rundir/../../.fault-bench-error-fired"
    if [[ ! -e $once ]]; then touch "$once"; return 1; fi
  fi
  if ! client "$BENCHMARK" --endpoints="$(client_endpoint)" "$@" >"$rundir/bench-$name.txt" 2>&1; then
    mark "fail:$name"; return 1
  fi
  if grep -q 'Error distribution' "$rundir/bench-$name.txt"; then
    mark "errors:$name"; return 1
  fi
  mark "end:$name"
}

# ---- S4 runs its own etcd (rw-benchmark.sh) ----------------------------------------
if [[ $SCENARIO_SELF_MANAGED == 1 ]]; then
  if scenario_client "$rundir" "$variant" "$bin"; then
    finish ok "" 0
  else
    rc=$?
    ((rc == 20)) && finish fatal "${SCENARIO_FAIL_REASON:-scenario guard failed}" 20
    ((rc == 11)) && finish invalid "${SCENARIO_FAIL_REASON:-invalid output}" 11
    finish failed "${SCENARIO_FAIL_REASON:-client step failed}" 10
  fi
fi

# ---- 1-2. Fresh data dir, cold cache -------------------------------------------------
"$HERE/stop-etcd.sh"
rm -rf "$DATA_DIR"
mkdir -p "$DATA_DIR"
if [[ $SCENARIO_GOLDEN != none ]]; then
  src="$GOLDEN_ROOT/$SCENARIO_GOLDEN"
  [[ -d $src/member ]] || finish fatal "golden $src missing" 20
  t0=$SECONDS
  cp -a "$src/." "$DATA_DIR/"
  python3 "$HERE/runjson.py" set "$RJ" "restore_s=$((SECONDS - t0))"
fi
sync
if [[ $MODE != mac ]]; then
  if echo 3 >/proc/sys/vm/drop_caches 2>/dev/null; then
    python3 "$HERE/runjson.py" set "$RJ" "dropped_caches=true"
  else
    [[ $MODE == vm ]] && finish fatal "cannot drop caches (not root?)" 20
    python3 "$HERE/runjson.py" set "$RJ" "dropped_caches=false"
  fi
fi

# ---- 3-4. Start etcd, health, variant guard, collector --------------------------------
mark etcd-start
pid="" cg=""
read -r pid cg < <("$HERE/start-etcd.sh" "$bin" "$start_cap" "$rundir/etcd.log") || true
[[ -n $pid ]] || finish failed "etcd did not start" 10
python3 "$HERE/runjson.py" set "$RJ" "etcd_pid=$pid" "cgroup=${cg:-}"
wait_health || finish failed "etcd not healthy within ${HEALTH_TIMEOUT_S}s" 10
mark etcd-healthy

if [[ $cap != infinity && $CAP_AT == healthy && $MODE == vm ]]; then
  t0=$SECONDS
  systemctl set-property --runtime etcd-bench.service "MemoryMax=$cap" MemorySwapMax=0
  # Wait for reclaim to bring the cgroup under the cap.
  while (($(cat "$cg/memory.current") > cap)) && ((SECONDS - t0 < 600)); do sleep 1; done
  (($(cat "$cg/memory.current") <= cap)) || finish failed "cgroup still above cap after 600s" 10
  [[ $(cat "$cg/memory.max") == "$cap" ]] || finish fatal "memory.max is $(cat "$cg/memory.max"), expected $cap" 20
  python3 "$HERE/runjson.py" set "$RJ" "cap_apply_s=$((SECONDS - t0))"
  mark cap-applied
fi

if [[ $MODE != mac ]]; then
  flags=$(python3 "$HERE/vmflags.py" "$pid" member/snap/db) || finish failed "db mapping not found in smaps" 10
  has_rr=false
  [[ " $flags " == *" rr "* ]] && has_rr=true
  python3 "$HERE/runjson.py" set "$RJ" "db_vmflags=$flags" "db_has_rr=$has_rr"
  if [[ $variant == control && $has_rr != true ]] || [[ $variant == treatment && $has_rr != false ]]; then
    finish fatal "variant guard: $variant has db VmFlags [$flags]" 20
  fi
  dev=""
  if [[ $MODE == vm ]]; then dev=$(basename "$(findmnt -no SOURCE "$DATA_MOUNT")"); fi
  client python3 "$HERE/collect.py" --pid "$pid" --cgroup "${cg:-}" --dev "$dev" --out "$rundir/samples.csv" &
  collector_pid=$!
fi
curl -fsS --max-time 10 "http://127.0.0.1:$METRICS_PORT/metrics" >"$rundir/metrics-start.txt" 2>/dev/null || true

# ---- 5. Client work ------------------------------------------------------------------
mark client-start
if ! scenario_client "$rundir" "$variant"; then
  finish failed "${SCENARIO_FAIL_REASON:-client step failed}" 10
fi
mark client-end

# ---- 6. Post-run checks ---------------------------------------------------------------
kill -0 "$pid" 2>/dev/null || finish failed "etcd exited during the run" 10
if [[ -n ${cg:-} && -f $cg/memory.events ]]; then
  ooms=$(awk '$1=="oom_kill"{print $2}' "$cg/memory.events")
  python3 "$HERE/runjson.py" set "$RJ" "oom_kill=${ooms:-0}"
  ((${ooms:-0} == 0)) || finish invalid "oom_kill=$ooms in etcd cgroup" 11
fi
if [[ -f $rundir/samples.csv ]]; then
  if ! python3 "$HERE/postcheck.py" "$rundir/samples.csv" --max-mibps "$GP3_MAX_MIBPS" >"$rundir/postcheck.txt"; then
    finish invalid "data volume at its throughput limit for the whole run" 11
  fi
fi
finish ok "" 0
