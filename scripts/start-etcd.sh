#!/usr/bin/env bash
# Start one etcd variant on $DATA_DIR, optionally under a memory cap.
#
# Usage: start-etcd.sh BINARY CAP LOGFILE
#   CAP is bytes, or "infinity" for no cap.
# Prints "PID CGROUP_DIR" on stdout (CGROUP_DIR empty when there is none).
#
# On a VM, etcd runs as a transient systemd service (etcd-bench.service) so that
# MemoryMax/MemorySwapMax apply to it alone and CPUAffinity pins it to $ETCD_CPUS.
# Locally (mac/linux modes) it runs as a plain background process.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
load_runtime_env

bin=$1 cap=$2 logfile=$3
[[ -x $bin ]] || die "no etcd binary at $bin"
mapfile -t flags < <(etcd_flags "$logfile")
mkdir -p "$STATE_DIR"

if [[ $MODE == vm ]]; then
  "$(dirname "$0")/stop-etcd.sh"
  props=(-p Type=exec -p "MemoryMax=$cap" -p MemorySwapMax=0 -p LimitNOFILE=1048576)
  if [[ -n ${ETCD_CPUS:-} ]]; then
    props+=(-p "CPUAffinity=${ETCD_CPUS//,/ }")
  fi
  systemd-run --quiet --unit=etcd-bench --collect "${props[@]}" "$bin" "${flags[@]}"
  pid=$(systemctl show -p MainPID --value etcd-bench)
  cg="/sys/fs/cgroup$(systemctl show -p ControlGroup --value etcd-bench)"
  [[ $pid != 0 && -d $cg ]] || die "etcd-bench did not start (pid=$pid cgroup=$cg)"
else
  "$(dirname "$0")/stop-etcd.sh"
  nohup "$bin" "${flags[@]}" >/dev/null 2>&1 &
  pid=$!
  cg=""
  if [[ $MODE == linux && -f /sys/fs/cgroup/memory.current ]]; then
    cg=/sys/fs/cgroup
  fi
fi
echo "$pid" >"$STATE_DIR/etcd.pid"
echo "$pid $cg"
