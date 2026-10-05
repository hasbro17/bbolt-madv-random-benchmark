#!/usr/bin/env bash
# Build a golden data dir with the control binary and no memory cap (PLAN P4).
#
# Usage:
#   make-golden.sh --name history --keys 500000 --passes 4 [--compacted-name compacted]
#   make-golden.sh --probe --keys 50000      load one pass, print db bytes per key-revision
#
# Each pass writes every key once (`benchmark put --sequential-keys`), so a golden with
# P passes holds K live keys and K*(P-1) older revisions. The golden is the stopped data
# dir copied to $GOLDEN_ROOT/<name>. With --compacted-name, a copy is compacted at its
# current revision (no defrag) and saved as a second golden. Facts go to dataset.env as
# GOLDEN_<name>_{KEYS,REV,DB_BYTES,DB_SHA,COMPACT_REV}.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/lib.sh"
load_runtime_env

name="" keys="" passes=1 compacted="" probe="" clients=64 conns=16
while (($#)); do
  case $1 in
    --name) name=$2; shift 2 ;;
    --keys) keys=$2; shift 2 ;;
    --passes) passes=$2; shift 2 ;;
    --compacted-name) compacted=$2; shift 2 ;;
    --probe) probe=1; shift ;;
    --clients) clients=$2; shift 2 ;;
    --conns) conns=$2; shift 2 ;;
    *) die "unknown arg $1" ;;
  esac
done
[[ -n $keys && ( -n $name || -n $probe ) ]] || die "need --keys and --name (or --probe)"
bin="$VARIANTS_ROOT/control/etcd"
ETCDCTL="$VARIANTS_ROOT/control/etcdctl"
BENCHMARK="$VARIANTS_ROOT/control/benchmark"
work="$GOLDEN_ROOT/.build-${name:-probe}"
mkdir -p "$work" "$ENV_DIR"

ctl() { "$ETCDCTL" --endpoints="$(client_endpoint)" --command-timeout=3600s "$@"; }
status_field() {
  ctl endpoint status -w json | python3 -c 'import json,sys; s=json.load(sys.stdin)[0]["Status"]; print({"rev": s["header"]["revision"], "db": s["dbSize"]}[sys.argv[1]])' "$1"
}
key_count() {
  ctl get "" --prefix --count-only -w fields | awk -F' : ' '$1=="\"Count\"" {print $2}'
}
start() {
  local pid
  read -r pid _ < <("$HERE/start-etcd.sh" "$bin" infinity "$work/etcd-$1.log")
  [[ -n $pid ]] || die "etcd did not start"
  wait_health || die "etcd not healthy"
}

"$HERE/stop-etcd.sh"
rm -rf "$DATA_DIR"
mkdir -p "$DATA_DIR"
start load
for ((p = 1; p <= passes; p++)); do
  log "pass $p/$passes: $keys keys x $VAL_SIZE B"
  "$BENCHMARK" --endpoints="$(client_endpoint)" --clients="$clients" --conns="$conns" put \
    --sequential-keys --key-space-size="$keys" --total="$keys" \
    --key-size="$KEY_SIZE" --val-size="$VAL_SIZE" >"$work/pass-$p.txt" 2>&1 || die "put pass $p failed"
  ! grep -q 'Error distribution' "$work/pass-$p.txt" || die "put pass $p reported errors"
done
rev=$(status_field rev)
db=$(status_field db)
count=$(key_count)
log "loaded: keys=$count rev=$rev db_bytes=$db"
if [[ -n $probe ]]; then
  per=$(python3 -c "print(round($db / ($keys * $passes)))")
  echo "PROBE keys=$keys passes=$passes db_bytes=$db bytes_per_key_revision=$per"
  "$HERE/stop-etcd.sh"
  exit 0
fi
"$HERE/stop-etcd.sh"

save_golden() {
  local dest="$GOLDEN_ROOT/$1"
  rm -rf "$dest"
  mkdir -p "$dest"
  cp -a "$DATA_DIR/." "$dest/"
  sync
  printf '%s' "$(sha256_of "$dest/member/snap/db")"
}

set_env() {
  local k=$1 v=$2 f="$ENV_DIR/dataset.env"
  touch "$f"
  grep -v "^$k=" "$f" >"$f.tmp" || true
  echo "$k=$v" >>"$f.tmp"
  mv "$f.tmp" "$f"
}

sha=$(save_golden "$name")
set_env "GOLDEN_${name}_KEYS" "$count"
set_env "GOLDEN_${name}_REV" "$rev"
set_env "GOLDEN_${name}_DB_BYTES" "$(wc -c <"$GOLDEN_ROOT/$name/member/snap/db" | tr -d ' ')"
set_env "GOLDEN_${name}_DB_SHA" "$sha"
set_env "GOLDEN_${name}_COMPACT_REV" 0
log "saved golden $name (db sha $sha)"

if [[ -n $compacted ]]; then
  start compact
  log "compacting a copy at rev $rev (no defrag)"
  ctl compact "$rev" --physical >"$work/compact.txt" 2>&1 || die "compact failed"
  "$HERE/stop-etcd.sh"
  csha=$(save_golden "$compacted")
  set_env "GOLDEN_${compacted}_KEYS" "$count"
  set_env "GOLDEN_${compacted}_REV" "$rev"
  set_env "GOLDEN_${compacted}_DB_BYTES" "$(wc -c <"$GOLDEN_ROOT/$compacted/member/snap/db" | tr -d ' ')"
  set_env "GOLDEN_${compacted}_DB_SHA" "$csha"
  set_env "GOLDEN_${compacted}_COMPACT_REV" "$rev"
  log "saved golden $compacted (db sha $csha)"
fi
cat "$ENV_DIR/dataset.env"
