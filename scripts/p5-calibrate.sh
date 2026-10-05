#!/usr/bin/env bash
# PLAN P5 steps 1-3 on VM A, unattended (as root). Writes everything under
# $RESULTS_ROOT/_calibration/ and $ENV_DIR/baseline.env, then prints the summary table.
# The cap is chosen afterwards from the summary (P5 step 4) and written to cap.env.
#
# Usage: p5-calibrate.sh [CAP_FRACTIONS]     default "0.60 0.40 0.25"
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/lib.sh"
load_runtime_env
fractions=${1:-"0.60 0.40 0.25"}

# 1. Baseline: control on the history golden, no cap; anon and file memory once settled.
# SKIP_BASELINE=1 / SKIP_NOCAP=1 reuse earlier results after a restart.
if [[ ${SKIP_BASELINE:-} == 1 ]]; then
  # shellcheck disable=SC1091
  source "$ENV_DIR/baseline.env"
  anon=$BASELINE_ANON_BYTES db=$BASELINE_DB_BYTES
else
"$HERE/stop-etcd.sh"
rm -rf "$DATA_DIR"
mkdir -p "$DATA_DIR"
cp -a "$GOLDEN_ROOT/history/." "$DATA_DIR/"
sync
echo 3 >/proc/sys/vm/drop_caches
read -r pid cg < <("$HERE/start-etcd.sh" "$VARIANTS_ROOT/control/etcd" infinity /tmp/baseline-etcd.log)
wait_health || die "baseline etcd not healthy"
sleep 60
anon=$(awk '$1=="anon"{print $2}' "$cg/memory.stat")
file=$(awk '$1=="file"{print $2}' "$cg/memory.stat")
rss=$(awk '{print $2 * 4096}' "/proc/$pid/statm")
"$HERE/stop-etcd.sh"
db=${GOLDEN_history_DB_BYTES:?}
{
  echo "BASELINE_ANON_BYTES=$anon"
  echo "BASELINE_FILE_BYTES=$file"
  echo "BASELINE_RSS_BYTES=$rss"
  echo "BASELINE_DB_BYTES=$db"
} >"$ENV_DIR/baseline.env"
log "baseline: anon=$((anon / 2**20)) MiB file=$((file / 2**20)) MiB rss=$((rss / 2**20)) MiB db=$((db / 2**20)) MiB"
fi

# 2. No-cap reference: 2 runs per variant, interleaved.
if [[ ${SKIP_NOCAP:-} != 1 ]]; then
  for v in control treatment control treatment; do
    "$HERE/calibrate.sh" nocap "$v" none >/dev/null
  done
fi

# 3. Cap sweep on control: anon + fraction of the db size.
for f in $fractions; do
  cap=$(python3 -c "print(int($anon + $f * $db))")
  "$HERE/calibrate.sh" "cap-$f-$cap" control "$cap" >/dev/null
done
"$HERE/calibrate.sh" --summary
