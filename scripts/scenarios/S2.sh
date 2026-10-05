# shellcheck shell=bash disable=SC2034
# S2: reads under the memory cap, from a cold cache (no warm-up), then a 10 minute mixed
# point + range pass at fixed rates so reclaim happens during measurement.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
SCENARIO_GOLDEN=history
SCENARIO_CAPPED=1
SCENARIO_MGLRU=on
S2_MIXED_S=${S2_MIXED_S:-600}

scenario_client() {
  local duration=$S2_MIXED_S keys a b rc=0
  [[ -n ${SMOKE:-} ]] && duration=60
  keys=$(golden_keys)
  read_suite || return 1
  bench_step mixed-stm stm --keys="$keys" --keys-per-txn=1 --txn-wr-percent=0 \
    --isolation=c --val-size=8 --rate=2000 --total=$((2000 * duration)) --clients=64 --conns=16 &
  a=$!
  bench_step mixed-range500 range "" --prefix --limit=500 --consistency=l \
    --rate=20 --total=$((20 * duration)) --clients=4 --conns=2 &
  b=$!
  wait "$a" || rc=1
  wait "$b" || rc=1
  return "$rc"
}
