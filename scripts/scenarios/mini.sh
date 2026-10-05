# shellcheck shell=bash disable=SC2034
# Mini smoke (PLAN P3 step 8 and local tests): tiny golden, tiny cap, a short random read
# load and one compaction, through the full run machinery.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
SCENARIO_GOLDEN=mini
SCENARIO_CAPPED=1
SCENARIO_MGLRU=${MINI_MGLRU:-on}
CAP_OVERRIDE=${CAP_OVERRIDE:-268435456}

scenario_client() {
  local rundir=$1 keys rev
  keys=$(golden_keys)
  rev=$(golden_rev)
  bench_step stm-mini stm --keys="$keys" --keys-per-txn=1 --txn-wr-percent=0 \
    --isolation=c --val-size=8 --rate=500 --total="${MINI_READS:-30000}" --clients=16 --conns=4 || return 1
  bench_step range500-mini range "" --prefix --limit=500 --total=50 --clients=4 --conns=2 || return 1
  printf 'etcdctl compact %s --physical\n' "$rev" >>"$rundir/commands.txt"
  mark compact-start
  client "$ETCDCTL" --endpoints="$(client_endpoint)" --command-timeout=600s compact "$rev" --physical \
    >"$rundir/etcdctl-compact.txt" 2>&1 || { SCENARIO_FAIL_REASON="etcdctl compact failed"; return 1; }
  wait_log "$rundir/etcd.log" 'finished scheduled compaction' 120 || { SCENARIO_FAIL_REASON="no compaction log line"; return 1; }
  mark compact-end
}
