# shellcheck shell=bash disable=SC2034
# S1: reads with no memory cap, after one warm-up full list (not measured).
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
SCENARIO_GOLDEN=history
SCENARIO_CAPPED=0
SCENARIO_MGLRU=on

scenario_client() {
  bench_step warmup-list range "" --prefix --paginate --total=1 --clients=1 --conns=1 || return 1
  read_suite
}
