#!/usr/bin/env bash
# PLAN P3 step 7, on every VM as root: start each variant on a scratch data dir and check
# the db mapping's VmFlags: control must have rr (MADV_RANDOM), treatment must not.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/lib.sh"
load_runtime_env
export DATA_DIR="$DATA_MOUNT/verify-etcd"
out="$ENV_DIR/vmflags.txt"
: >"$out"
fail=0
for v in control treatment; do
  "$HERE/stop-etcd.sh"
  rm -rf "$DATA_DIR"
  [[ $(sha256_of "$VARIANTS_ROOT/$v/etcd") == "$(expected_sha "$v" etcd)" ]] || die "$v checksum mismatch"
  read -r pid _ < <("$HERE/start-etcd.sh" "$VARIANTS_ROOT/$v/etcd" infinity "/tmp/verify-$v.log")
  wait_health 120 || die "$v not healthy"
  flags=$(python3 "$HERE/vmflags.py" "$pid" member/snap/db)
  echo "$v: $flags" | tee -a "$out"
  case $v in
    control) [[ " $flags " == *" rr "* ]] || { log "control lacks rr"; fail=1; } ;;
    treatment) [[ " $flags " != *" rr "* ]] || { log "treatment has rr"; fail=1; } ;;
  esac
done
"$HERE/stop-etcd.sh"
rm -rf "$DATA_DIR"
((fail == 0)) || die "variant check failed"
log "variants verified: control has rr, treatment does not"
