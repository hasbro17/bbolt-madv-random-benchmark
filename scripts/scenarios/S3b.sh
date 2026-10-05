# shellcheck shell=bash disable=SC2034
# S3b: steady writes with periodic compaction, plus a background random reader that
# churns the page cache between compactions, under the memory cap.
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
SCENARIO_GOLDEN=compacted
SCENARIO_CAPPED=1
SCENARIO_MGLRU=on
S3B_DURATION_S=${S3B_DURATION_S:-900}
S3B_PUT_RATE=${S3B_PUT_RATE:-1000}
S3B_READ_RATE=${S3B_READ_RATE:-500}

scenario_client() {
  local duration=$S3B_DURATION_S keys put_pid read_pid rc=0
  [[ -n ${SMOKE:-} ]] && duration=120
  keys=$(golden_keys)
  bench_step put-compact put --key-size="$KEY_SIZE" --val-size="$VAL_SIZE" \
    --key-space-size="$keys" --rate="$S3B_PUT_RATE" --total=$((S3B_PUT_RATE * duration)) \
    --compact-interval=60s --compact-index-delta=$((S3B_PUT_RATE * 30)) \
    --clients=16 --conns=4 &
  put_pid=$!
  bench_step stm-background stm --keys="$keys" --keys-per-txn=1 --txn-wr-percent=0 \
    --isolation=c --val-size=8 --rate="$S3B_READ_RATE" --total=$((S3B_READ_RATE * duration)) \
    --clients=16 --conns=4 &
  read_pid=$!
  wait "$put_pid" || rc=1
  wait "$read_pid" || rc=1
  return "$rc"
}
