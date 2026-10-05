#!/usr/bin/env bash
# PLAN P2: host setup and preflight, as root on a VM. Resumable: each step leaves a marker
# in $STATE_DIR/setup.<step>.done and is skipped next time.
#
# Usage: host-setup.sh DATA_VOLUME_ID      e.g. vol-0123abcd (the /dev/sdf gp3 volume)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/lib.sh"
[[ $(id -u) == 0 ]] || die "run as root"
data_vol=${1:?data volume id}
mkdir -p "$STATE_DIR" "$ENV_DIR"

step() { # step NAME FUNCTION
  if [[ -f $STATE_DIR/setup.$1.done ]]; then log "setup $1: already done"; return; fi
  log "setup $1"
  "$2"
  touch "$STATE_DIR/setup.$1.done"
}

packages() {
  # Exclude the packages that would change the running kernel (kernel-headers is fine).
  # No gcc: every build is CGO_ENABLED=0.
  dnf install -y --setopt=exclude=kernel,kernel-core,kernel-modules,kernel-modules-core,kernel-modules-extra \
    git make tmux jq bc sysstat xfsprogs python3 rsync curl util-linux procps-ng findutils tar >/dev/null
}

golang() {
  local v=1.27.1
  curl -fsSL "https://go.dev/dl/go$v.linux-amd64.tar.gz" -o /tmp/go.tgz
  rm -rf /usr/local/go
  tar -C /usr/local -xzf /tmp/go.tgz
  # shellcheck disable=SC2016 # literal $PATH for the profile script
  echo 'export PATH=/usr/local/go/bin:$PATH' >/etc/profile.d/go.sh
  /usr/local/go/bin/go version
}

datavol() {
  local serial=${data_vol//-/} dev
  dev=$(lsblk -dnpo NAME,SERIAL | awk -v s="$serial" '$2==s {print $1}')
  [[ -n $dev ]] || die "no block device with serial $serial"
  if ! blkid "$dev" >/dev/null 2>&1; then mkfs.xfs -q "$dev"; fi
  mkdir -p "$DATA_MOUNT"
  local uuid
  uuid=$(blkid -s UUID -o value "$dev")
  grep -q "$uuid" /etc/fstab || echo "UUID=$uuid $DATA_MOUNT xfs defaults,noatime,nofail 0 2" >>/etc/fstab
  systemctl daemon-reload
  mountpoint -q "$DATA_MOUNT" || mount "$DATA_MOUNT"
  mountpoint -q "$DATA_MOUNT" || die "$DATA_MOUNT not mounted"
  echo "DATA_DEV=$(basename "$dev")" >"$ENV_DIR/datadev.env"
}

noswap() {
  swapoff -a
  sed -i -E 's@^([^#].*\sswap\s.*)$@# \1@' /etc/fstab
  [[ -z $(swapon --show) ]] || die "swap still active"
}

cpus() {
  # Whole physical cores, both hyperthreads each: first half of the cores for etcd, the
  # second half for the client and collector.
  python3 - "$ENV_DIR/cpus.env" <<'PY'
import collections, subprocess, sys
cores = collections.OrderedDict()
for line in subprocess.run(["lscpu", "-p=CPU,CORE,SOCKET"], capture_output=True, text=True).stdout.splitlines():
    if line.startswith("#"):
        continue
    cpu, core, sock = line.split(",")
    cores.setdefault((int(sock), int(core)), []).append(int(cpu))
keys = sorted(cores)
half = len(keys) // 2
etcd = sorted(c for k in keys[:half] for c in cores[k])
client = sorted(c for k in keys[half:] for c in cores[k])
with open(sys.argv[1], "w") as f:
    f.write(f"ETCD_CPUS={','.join(map(str, etcd))}\nCLIENT_CPUS={','.join(map(str, client))}\n")
print(open(sys.argv[1]).read())
PY
}

quiet() {
  local u quieted=()
  for u in fstrim.timer dnf-makecache.timer sysstat-collect.timer sysstat-summary.timer sysstat.service \
    insights-client.timer insights-client-boot.service rhsmcertd.service mlocate-updatedb.timer \
    plocate-updatedb.timer unbound-anchor.timer; do
    if systemctl list-unit-files "$u" --no-legend 2>/dev/null | grep -q .; then
      systemctl disable --now "$u" >/dev/null 2>&1 || true
      systemctl mask "$u" >/dev/null 2>&1 || true
      quieted+=("$u")
    fi
  done
  printf '%s\n' "${quieted[@]}" >"$ENV_DIR/quieted.txt"
  systemctl list-timers --all --no-pager >"$ENV_DIR/timers-after-quiet.txt"
}

unit() {
  cat >/etc/systemd/system/bench-matrix.service <<'UNIT'
[Unit]
Description=madv benchmark matrix for this VM's scenario group
After=network-online.target local-fs.target
RequiresMountsFor=/data
StartLimitIntervalSec=3600
StartLimitBurst=5

[Service]
Type=simple
EnvironmentFile=/opt/bench/env/role.env
EnvironmentFile=-/opt/bench/env/matrix.env
ExecStart=/bin/bash -c 'exec /opt/bench/scripts/run-matrix.sh "$VM_ROLE"'
Restart=on-failure
RestartSec=60
# 20 = a fatal guard fired; restarting would only hit it again.
RestartPreventExitStatus=20
# Not StandardOutput=append:... : SELinux does not let systemd (init_t) open a usr_t
# file under /opt. run-matrix.sh appends to MATRIX_LOG itself; systemd keeps the journal.
Environment=MATRIX_LOG=/opt/bench/state/matrix.log

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
}

preflight() {
  local f="$ENV_DIR/preflight.txt" dev
  dev=$(awk -F= '$1=="DATA_DEV" {print $2}' "$ENV_DIR/datadev.env")
  {
    echo "== date"; date -u
    echo "== uname"; uname -a
    echo "== os-release"; cat /etc/os-release
    echo "== lru_gen"; cat /sys/kernel/mm/lru_gen/enabled
    echo "== cgroup fs"; stat -fc %T /sys/fs/cgroup
    echo "== lscpu"; lscpu
    echo "== free"; free -g
    echo "== lsblk"; lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,SERIAL,FSTYPE
    echo "== readahead (data dev)"; blockdev --getra "/dev/$dev"
    echo "== read_ahead_kb"; cat "/sys/block/$dev/queue/read_ahead_kb"
    echo "== THP"; cat /sys/kernel/mm/transparent_hugepage/enabled
    echo "== tuned"; tuned-adm active 2>/dev/null || echo none
    echo "== governor"; cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo n/a
    echo "== swap"; swapon --show
    echo "== quieted"; cat "$ENV_DIR/quieted.txt"
    echo "== cpus"; cat "$ENV_DIR/cpus.env"
    echo "== sysctl vm"; sysctl vm
  } >"$f" 2>&1
}

gate() {
  local k ok=1
  k=$(uname -r)
  python3 -c "import sys; v=tuple(int(x) for x in '$k'.split('.')[:2]); sys.exit(0 if v >= (6, 4) else 1)" \
    || { log "GATE FAIL kernel $k < 6.4"; ok=0; }
  [[ $(cat /sys/kernel/mm/lru_gen/enabled) == 0x0007 ]] || { log "GATE FAIL MGLRU $(cat /sys/kernel/mm/lru_gen/enabled)"; ok=0; }
  [[ $(stat -fc %T /sys/fs/cgroup) == cgroup2fs ]] || { log "GATE FAIL not cgroup v2"; ok=0; }
  mountpoint -q "$DATA_MOUNT" || { log "GATE FAIL $DATA_MOUNT not mounted"; ok=0; }
  [[ -z $(swapon --show) ]] || { log "GATE FAIL swap active"; ok=0; }
  while read -r u; do
    [[ -z $u ]] && continue
    systemctl is-active --quiet "$u" && { log "GATE FAIL $u still active"; ok=0; }
  done <"$ENV_DIR/quieted.txt"
  echo "KERNEL=$k" >"$ENV_DIR/kernel.env"
  ((ok)) || die "host gate failed"
  log "host gate passed (kernel $k, MGLRU on, cgroup v2, $DATA_MOUNT mounted, no swap, timers quiet)"
}

step packages packages
step golang golang
step datavol datavol
step noswap noswap
step cpus cpus
step quiet quiet
step unit unit
preflight
gate
