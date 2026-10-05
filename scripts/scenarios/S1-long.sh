# shellcheck shell=bash disable=SC2034
# S1-long: S1 with 10x the requests per random point-read step (supplementary, user
# 2026-10-03). In S1 the stm steps (20k/80k/320k requests) last only 0.5/1.2/4.3 s; here
# they last ~5-45 s. Range and list steps are the same as S1. read_suite is redefined here
# rather than in _common.sh so the scenario_rev of every other scenario stays unchanged.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
SCENARIO_GOLDEN=history
SCENARIO_CAPPED=0
SCENARIO_MGLRU=on
S1L_STM_SCALE=${S1L_STM_SCALE:-10}

read_suite() {
  local keys cc c n
  keys=$(golden_keys)
  for cc in "16 4" "64 16" "256 64"; do
    read -r c n <<<"$cc"
    bench_step "stm-c$c" stm --keys="$keys" --keys-per-txn=1 --txn-wr-percent=0 \
      --isolation=c --val-size=8 --total="$(scaled $((20000 * S1L_STM_SCALE * c / 16)))" \
      --clients="$c" --conns="$n" || return 1
    bench_step "range500-l-c$c" range "" --prefix --limit=500 --consistency=l \
      --total="$(scaled $((1000 * c / 16)))" --clients="$c" --conns="$n" || return 1
    bench_step "range500-s-c$c" range "" --prefix --limit=500 --consistency=s \
      --total="$(scaled $((1000 * c / 16)))" --clients="$c" --conns="$n" || return 1
  done
  local lists=2
  [[ -n ${SMOKE:-} ]] && lists=1
  bench_step "list-paginate" range "" --prefix --paginate --total="$lists" --clients=1 --conns=1 || return 1
}

scenario_client() {
  bench_step warmup-list range "" --prefix --paginate --total=1 --clients=1 --conns=1 || return 1
  read_suite
}
