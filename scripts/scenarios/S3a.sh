# shellcheck shell=bash disable=SC2034
# S3a: one-shot cold compaction of the uncompacted history golden, under the memory cap.
# The headline A/B: same input and same work for both variants, one number per run.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
SCENARIO_GOLDEN=history
SCENARIO_CAPPED=1
SCENARIO_MGLRU=on

scenario_client() {
  local rundir=$1 settle=120 rev
  [[ -n ${SMOKE:-} ]] && settle=15
  rev=$(golden_rev)
  mark settle-start
  sleep "$settle"
  mark settle-end
  printf 'etcdctl compact %s --physical\n' "$rev" >>"$rundir/commands.txt"
  mark compact-start
  if ! client "$ETCDCTL" --endpoints="$(client_endpoint)" --command-timeout="${COMPACTION_TIMEOUT_S}s" \
    compact "$rev" --physical >"$rundir/etcdctl-compact.txt" 2>&1; then
    SCENARIO_FAIL_REASON="etcdctl compact failed"
    return 1
  fi
  mark compact-returned
  if ! wait_log "$rundir/etcd.log" 'finished scheduled compaction' 600; then
    SCENARIO_FAIL_REASON="no 'finished scheduled compaction' log line"
    return 1
  fi
  mark compact-end
}
