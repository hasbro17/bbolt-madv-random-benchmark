#!/usr/bin/env bash
# P0 step 8: run every scenario's client commands against a local etcd on the Mac, in
# smoke mode (no cap, no pinning, short durations), and keep the real outputs as parser
# fixtures in scripts/report/testdata/real/.
#
# Usage: run-local.sh [SCENARIO ...]     default: S3a S3b S2 S1 S4 mini
set -euo pipefail
SCRIPTS="$(cd "$(dirname "$0")/.." && pwd)"
BASE="$(cd "$SCRIPTS/.." && pwd)"
L="$BASE/local"
export MODE=mac SMOKE=1
export BENCH_ROOT="$L/mac-bench"
export RESULTS_ROOT="$BENCH_ROOT/results" STATE_DIR="$BENCH_ROOT/state" ENV_DIR="$BENCH_ROOT/env"
export VARIANTS_ROOT="$L/build-darwin-arm64" GOLDEN_ROOT="$L/mac-golden"
export DATA_MOUNT="$L/mac-data" DATA_DIR="$L/mac-data/etcd" ETCD_SRC="$L/etcd"
export CLIENT_PORT=23379 PEER_PORT=23380 METRICS_PORT=23381
# shellcheck source=../lib.sh
source "$SCRIPTS/lib.sh"

mkdir -p "$ENV_DIR" "$DATA_MOUNT" "$GOLDEN_ROOT"
cp "$VARIANTS_ROOT/versions.env" "$ENV_DIR/versions.env"
echo "CAP_BYTES=1073741824" >"$ENV_DIR/cap.env"

if [[ ! -d $GOLDEN_ROOT/history/member || ! -d $GOLDEN_ROOT/mini/member ]]; then
  "$SCRIPTS/make-golden.sh" --name history --keys 10000 --passes 3 --compacted-name compacted --clients 16 --conns 4
  "$SCRIPTS/make-golden.sh" --name mini --keys 5000 --passes 2 --clients 16 --conns 4
fi

scenarios=${*:-"S3a S3b S2 S1 S4 mini"}
rc=0
for s in $scenarios; do
  for v in control treatment; do
    rd="$RESULTS_ROOT/$s/pair-01/$v"
    rm -rf "$rd"
    if MINI_READS=3000 "$SCRIPTS/run-one.sh" "$s" "$v" "$rd"; then
      log "LOCAL OK $s/$v"
    else
      log "LOCAL FAIL $s/$v: $(python3 "$SCRIPTS/runjson.py" get "$rd/run.json" reason)"
      rc=1
    fi
  done
done

# Keep the real outputs (small text files only) as fixtures.
dest="$SCRIPTS/report/testdata/real"
rm -rf "$dest"
mkdir -p "$dest"
(cd "$RESULTS_ROOT" && find . -type f \( -name '*.txt' -o -name '*.json' -o -name '*.csv' -o -name '*.log' \) \
  -size -5M -print0 | while IFS= read -r -d '' f; do mkdir -p "$dest/$(dirname "$f")"; cp "$f" "$dest/$f"; done)
log "fixtures copied to $dest"
exit "$rc"
