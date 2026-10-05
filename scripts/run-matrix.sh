#!/usr/bin/env bash
# Run one VM's scenario group, pair by pair, resumably (PLAN P6 and "Failure handling").
#
# Usage: run-matrix.sh GROUP            GROUP is A..E (see config.env GROUP_*)
#        SMOKE=1 run-matrix.sh GROUP    1 short pair per scenario, into results/_smoke/
#        run-matrix.sh --scenarios "S3a S2" ...   explicit list (tests, targeted reruns)
#
# A pair = one control run + one treatment run, back to back, order counterbalanced
# (odd pairs control first, even pairs treatment first). A pair is done when both runs
# are ok at the current scenario_rev; done pairs are skipped, so restarting resumes at
# the first incomplete pair. A pair left half-finished by an interruption, and any
# failed attempt, is moved to <scenario>/_failed/ with a reason; it is never deleted.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/lib.sh"
load_runtime_env

RUN_ONE=${RUN_ONE:-$HERE/run-one.sh}
if [[ -n ${MATRIX_LOG:-} ]]; then exec > >(tee -a "$MATRIX_LOG") 2>&1; fi
scenarios=""
if [[ ${1:-} == --scenarios ]]; then
  scenarios=$2
else
  [[ -n ${1:-} ]] || die "usage: run-matrix.sh GROUP | --scenarios LIST"
  # MATRIX_SCENARIOS (from matrix.env) overrides the group, for P5b tests via the unit.
  scenarios=${MATRIX_SCENARIOS:-$(group_for "$1")}
  export VM_ROLE=${VM_ROLE:-$1}
fi
root=$RESULTS_ROOT
[[ -n ${SMOKE:-} ]] && root="$RESULTS_ROOT/_smoke"
mkdir -p "$root" "$STATE_DIR"

status() {
  python3 - "$STATE_DIR/matrix.status" "$@" <<'PY'
import json, sys, time
path, kv = sys.argv[1], dict(a.split("=", 1) for a in sys.argv[2:])
kv["ts"] = int(time.time())
open(path, "w").write(json.dumps(kv) + "\n")
PY
}

scenario_rev() {
  local id=$1 files=("$HERE/scenarios/$1.sh" "$HERE/scenarios/_common.sh")
  local base
  # shellcheck disable=SC2013
  for base in $(grep '^source' "$HERE/scenarios/$id.sh" | grep -o '[A-Za-z0-9_-]*\.sh' | sort -u); do
    [[ -f $HERE/scenarios/$base ]] && files+=("$HERE/scenarios/$base")
  done
  { cat "${files[@]}"; grep -v '^PAIRS_' "$HERE/config.env"; } | { sha256sum 2>/dev/null || shasum -a 256; } | cut -c1-12
}

run_ok() { [[ -f $1/run.json && $(python3 "$HERE/runjson.py" get "$1/run.json" status) == ok ]]; }

pair_done() {
  local pdir=$1 rev=$2
  [[ -f $pdir/pair.done ]] || return 1
  [[ $(python3 "$HERE/runjson.py" get "$pdir/pair.done" scenario_rev) == "$rev" ]] || return 1
  run_ok "$pdir/control" && run_ok "$pdir/treatment"
}

# Move a pair dir aside (interrupted, failed, or superseded by a scenario_rev change).
shelve() {
  local sdir=$1 pdir=$2 kind=$3 reason=$4 dest
  [[ -d $pdir ]] || return 0
  local base i=1
  base="$sdir/$kind/$(basename "$pdir")-$(date -u +%Y%m%dT%H%M%SZ)"
  dest=$base
  while [[ -e $dest ]]; do i=$((i + 1)); dest="$base-$i"; done
  mkdir -p "$sdir/$kind"
  mv "$pdir" "$dest"
  printf '%s\n' "$reason" >"$dest/REASON"
  log "moved $pdir -> $dest ($reason)"
}

mglru_restore=""
cleanup() { [[ -n $mglru_restore ]] && "$HERE/mglru.sh" on || true; }
trap cleanup EXIT

for scen in $scenarios; do
  sdir="$root/$scen"
  mkdir -p "$sdir"
  rm -f "$sdir/scenario.stopped"
  rev=$(scenario_rev "$scen")
  n=$(pairs_for "$scen")
  export SCENARIO_REV=$rev
  log "scenario $scen: $n pairs, scenario_rev=$rev"

  if [[ $scen == *mglru-off* && $MODE == vm ]]; then
    "$HERE/mglru.sh" off
    mglru_restore=1
  fi

  for ((p = 1; p <= n; p++)); do
    pdir=$(printf '%s/pair-%02d' "$sdir" "$p")
    if pair_done "$pdir" "$rev"; then continue; fi
    if [[ -f $pdir/pair.done ]]; then
      shelve "$sdir" "$pdir" _superseded "scenario_rev changed to $rev"
    elif [[ -d $pdir ]]; then
      shelve "$sdir" "$pdir" _failed "interrupted before completion (resumed)"
    fi
    order="control treatment"
    ((p % 2 == 0)) && order="treatment control"
    done_pair=""
    for ((attempt = 1; attempt <= MAX_PAIR_ATTEMPTS; attempt++)); do
      ok=1
      for v in $order; do
        status "scenario=$scen" "pair=$p" "variant=$v" "attempt=$attempt" "state=running"
        set +e
        "$RUN_ONE" "$scen" "$v" "$pdir/$v"
        rc=$?
        set -e
        if ((rc == 20)); then
          reason=$(python3 "$HERE/runjson.py" get "$pdir/$v/run.json" reason)
          shelve "$sdir" "$pdir" _failed "fatal: $reason"
          status "scenario=$scen" "pair=$p" "state=fatal" "reason=$reason"
          printf '%s %s pair %s: %s\n' "$(date -u +%FT%TZ)" "$scen" "$p" "$reason" >"$STATE_DIR/matrix.stopped"
          log "FATAL: fatal guard in $scen pair $p ($v): $reason; matrix stopped"
          exit 20
        fi
        if ((rc != 0)); then
          reason=$(python3 "$HERE/runjson.py" get "$pdir/$v/run.json" reason)
          shelve "$sdir" "$pdir" _failed "attempt $attempt: $v exited $rc: $reason"
          ok=0
          break
        fi
      done
      if ((ok)); then
        python3 "$HERE/runjson.py" set "$pdir/pair.done" "scenario_rev=$rev" "order=$order" \
          "attempts=$attempt" "done_ts=$(date +%s)" "vm=${VM_ROLE:-local}"
        done_pair=1
        break
      fi
    done
    if [[ -z $done_pair ]]; then
      msg="pair $p failed $MAX_PAIR_ATTEMPTS attempts; scenario stopped for diagnosis"
      printf '%s\n' "$msg" >"$sdir/scenario.stopped"
      status "scenario=$scen" "pair=$p" "state=scenario-stopped"
      log "$scen: $msg"
      break
    fi
  done

  if [[ -n $mglru_restore ]]; then
    "$HERE/mglru.sh" on
    mglru_restore=""
  fi
done

status "state=group-finished" "scenarios=$scenarios"
touch "$root/.group-${VM_ROLE:-local}.finished"
log "group finished: $scenarios"
