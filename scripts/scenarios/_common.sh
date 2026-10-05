# shellcheck shell=bash
# Helpers shared by scenario files. Sourced by run-one.sh through a scenario file, so
# lib.sh, client(), bench_step(), mark() and $RUNDIR are already defined.

# Number of keys in the golden in use (dataset.env), falling back to the config default.
golden_keys() {
  local var="GOLDEN_${SCENARIO_GOLDEN}_KEYS"
  printf '%s' "${!var:-$GOLDEN_KEYS}"
}

golden_rev() {
  local var="GOLDEN_${SCENARIO_GOLDEN}_REV"
  printf '%s' "${!var:?no $var in dataset.env}"
}

# Scale a request count down in smoke mode so every command still runs, briefly.
scaled() { if [[ -n ${SMOKE:-} ]]; then echo $(( ($1 + 19) / 20 )); else echo "$1"; fi; }

# wait_log FILE PATTERN TIMEOUT_S: wait for a line matching PATTERN in FILE.
wait_log() {
  local file=$1 pat=$2 deadline=$((SECONDS + $3))
  while ((SECONDS < deadline)); do
    grep -q -- "$pat" "$file" 2>/dev/null && return 0
    sleep 1
  done
  return 1
}

# The read suite used by S1 and S2 (and the mini smoke, scaled down):
#   - random point reads via `benchmark stm` (one existing key per transaction, no writes)
#   - bounded range reads of 500 keys, linearizable and serializable
#   - one paginated full list (all keys, 10k-key pages)
read_suite() {
  local keys cc c n
  keys=$(golden_keys)
  for cc in "16 4" "64 16" "256 64"; do
    read -r c n <<<"$cc"
    bench_step "stm-c$c" stm --keys="$keys" --keys-per-txn=1 --txn-wr-percent=0 \
      --isolation=c --val-size=8 --total="$(scaled $((20000 * c / 16)))" \
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
