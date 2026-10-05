#!/usr/bin/env bash
# shellcheck disable=SC2016,SC2329 # check() evals single-quoted expressions on purpose
# P0 step 7: run-matrix.sh logic with a stand-in run-one.sh. No VM, no etcd.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS="$(cd "$HERE/.." && pwd)"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
export BENCH_ROOT="$T/bench" MODE=mac
export RESULTS_ROOT="$BENCH_ROOT/results" STATE_DIR="$BENCH_ROOT/state" ENV_DIR="$BENCH_ROOT/env"
mkdir -p "$ENV_DIR"
fails=0
pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
check() { if eval "$2"; then pass "$1"; else fail "$1"; fi; }

# Fake run-one: behaviour per scenario from FAKE_<scenario> (ok, fail-first, fail-always,
# fatal). Logs every call to $T/calls.
cat >"$T/fake-run-one.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
scen=$1 variant=$2 rundir=$3
mkdir -p "$rundir"
echo "$scen $variant $(basename "$(dirname "$rundir")")" >>"$FAKE_DIR/calls"
var="FAKE_${scen//-/_}"
mode=${!var:-ok}
rj() { python3 "$SCRIPTS/runjson.py" set "$rundir/run.json" "$@"; }
case $mode in
  ok) rj status=ok; exit 0 ;;
  fail-first)
    if [[ ! -e $FAKE_DIR/$scen.failed ]]; then touch "$FAKE_DIR/$scen.failed"; rj status=failed reason=injected; exit 10; fi
    rj status=ok; exit 0 ;;
  fail-always) rj status=failed reason=injected; exit 10 ;;
  fatal) rj status=fatal reason=injected-fatal; exit 20 ;;
esac
EOF
chmod +x "$T/fake-run-one.sh"
export RUN_ONE="$T/fake-run-one.sh" FAKE_DIR="$T" SCRIPTS
calls() { wc -l <"$T/calls" | tr -d ' '; }
: >"$T/calls"

# 1. Basic: pair count, alternation, counterbalanced order, pair.done.
PAIRS_S1=3 "$SCRIPTS/run-matrix.sh" --scenarios S1 2>/dev/null
check "3 pairs done" '[[ $(ls -d "$RESULTS_ROOT"/S1/pair-*/pair.done | wc -l) -eq 3 ]]'
check "6 runs" '[[ $(calls) -eq 6 ]]'
check "pair 1 control first" 'sed -n 1p "$T/calls" | grep -q "S1 control pair-01"'
check "pair 2 treatment first" 'sed -n 3p "$T/calls" | grep -q "S1 treatment pair-02"'
check "pair.done order recorded" 'grep -q "treatment control" "$RESULTS_ROOT/S1/pair-02/pair.done"'

# 2. Resume: nothing reruns when all pairs are done.
PAIRS_S1=3 "$SCRIPTS/run-matrix.sh" --scenarios S1 2>/dev/null
check "rerun is a no-op" '[[ $(calls) -eq 6 ]]'

# 3. Interrupted pair: a half-finished pair is shelved and rerun.
mkdir -p "$RESULTS_ROOT/S1/pair-04/control"
python3 "$SCRIPTS/runjson.py" set "$RESULTS_ROOT/S1/pair-04/control/run.json" status=ok
PAIRS_S1=4 "$SCRIPTS/run-matrix.sh" --scenarios S1 2>/dev/null
check "interrupted pair shelved" 'grep -q interrupted "$RESULTS_ROOT"/S1/_failed/pair-04-*/REASON'
check "interrupted pair rerun" '[[ -f $RESULTS_ROOT/S1/pair-04/pair.done && $(calls) -eq 8 ]]'

# 4. Retry: one failure, retried, attempt count 2.
: >"$T/calls"
FAKE_S2=fail-first PAIRS_S2=1 "$SCRIPTS/run-matrix.sh" --scenarios S2 2>/dev/null
check "retry shelved the failed attempt" '[[ $(ls -d "$RESULTS_ROOT"/S2/_failed/pair-01-* | wc -l) -eq 1 ]]'
check "retry succeeded on attempt 2" 'grep -q "\"attempts\": 2" "$RESULTS_ROOT/S2/pair-01/pair.done"'

# 5. Retry limit: scenario stops after 3 attempts, the next scenario still runs.
: >"$T/calls"
FAKE_S3b=fail-always PAIRS_S3b=2 PAIRS_S1=4 "$SCRIPTS/run-matrix.sh" --scenarios "S3b S1" 2>/dev/null
check "3 failed attempts kept" '[[ $(ls -d "$RESULTS_ROOT"/S3b/_failed/pair-01-* | wc -l) -eq 3 ]]'
check "scenario.stopped written" '[[ -f $RESULTS_ROOT/S3b/scenario.stopped ]]'
check "pair 2 of stopped scenario not attempted" '! grep -q "S3b .* pair-02" "$T/calls"'

# 6. Fatal guard stops the whole matrix.
: >"$T/calls"
if FAKE_S3a=fatal PAIRS_S3a=2 "$SCRIPTS/run-matrix.sh" --scenarios "S3a S1" 2>/dev/null; then
  fail "fatal exits non-zero"
else
  pass "fatal exits non-zero"
fi
check "matrix.stopped written" '[[ -f $STATE_DIR/matrix.stopped ]]'
check "nothing ran after fatal" '[[ $(calls) -eq 1 ]]'
rm -f "$STATE_DIR/matrix.stopped"

# 7. scenario_rev change supersedes done pairs.
python3 "$SCRIPTS/runjson.py" set "$RESULTS_ROOT/S1/pair-01/pair.done" scenario_rev=old
: >"$T/calls"
PAIRS_S1=4 "$SCRIPTS/run-matrix.sh" --scenarios S1 2>/dev/null
check "old-rev pair superseded" 'ls -d "$RESULTS_ROOT"/S1/_superseded/pair-01-* >/dev/null 2>&1'
check "only that pair reran" '[[ $(calls) -eq 2 ]]'

# 8. FAULT hooks are inert without SMOKE.
check "FAULT ignored without SMOKE" '! (unset SMOKE; FAULT=bench-error bash -c "source $SCRIPTS/lib.sh; fault_is bench-error")'
check "FAULT active with SMOKE" '(SMOKE=1 FAULT=bench-error bash -c "source $SCRIPTS/lib.sh; fault_is bench-error")'

# 9. SMOKE: 1 pair per scenario, separate results tree.
: >"$T/calls"
SMOKE=1 "$SCRIPTS/run-matrix.sh" --scenarios "S1 S2" 2>/dev/null
check "smoke runs 1 pair each" '[[ $(calls) -eq 4 ]]'
check "smoke results under _smoke" '[[ -f $RESULTS_ROOT/_smoke/S1/pair-01/pair.done && ! -d $RESULTS_ROOT/_smoke/S1/pair-02 ]]'

echo "failures: $fails"
exit "$fails"
