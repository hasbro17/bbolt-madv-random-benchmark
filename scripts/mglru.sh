#!/usr/bin/env bash
# Switch MGLRU off or on and verify the kernel's reading (PLAN P6, MGLRU-off arm).
# Usage: mglru.sh off|on|status
set -euo pipefail
source "$(dirname "$0")/lib.sh"

knob=/sys/kernel/mm/lru_gen/enabled
mkdir -p "$ENV_DIR"
case ${1:-status} in
  off) want=0x0000; echo n >"$knob" ;;
  on) want=0x0007; echo y >"$knob" ;;
  status) cat "$knob"; exit 0 ;;
  *) die "usage: mglru.sh off|on|status" ;;
esac
have=$(cat "$knob")
printf '%s %s -> %s\n' "$(date -u +%FT%TZ)" "$1" "$have" >>"$ENV_DIR/mglru.log"
[[ $have == "$want" ]] || die "MGLRU reads $have after '$1', expected $want"
log "MGLRU $1 ($have)"
