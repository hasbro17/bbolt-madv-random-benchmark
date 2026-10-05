# shellcheck shell=bash
# Common helpers. Source after `set -euo pipefail`.

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.env
source "$LIB_DIR/config.env"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# MODE: "vm" (systemd, cgroups, root), "linux" (plain Linux, e.g. a podman container),
# or "mac" (macOS, no /proc, no cgroups). Detected unless set.
if [[ -z "${MODE:-}" ]]; then
  if [[ "$(uname -s)" == "Darwin" ]]; then
    MODE=mac
  elif [[ -d /run/systemd/system ]] && command -v systemd-run >/dev/null; then
    MODE=vm
  else
    MODE=linux
  fi
fi

load_runtime_env() {
  local f
  for f in role cpus cap dataset versions datadev; do
    if [[ -f "$ENV_DIR/$f.env" ]]; then
      # shellcheck disable=SC1090
      source "$ENV_DIR/$f.env"
    fi
  done
}

sha256_of() {
  if command -v sha256sum >/dev/null; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

# Expected checksum for a variant binary from versions.env, e.g. SHA_treatment_etcd.
expected_sha() {
  local var="SHA_${1}_${2}"
  printf '%s' "${!var:-}"
}

free_gib() {
  df -Pk "$1" | awk 'NR==2 {printf "%d", $4/1024/1024}'
}

client_endpoint() { printf 'http://127.0.0.1:%s' "$CLIENT_PORT"; }

etcd_flags() {
  local logfile=$1
  printf '%s\n' \
    "--name=$ETCD_NAME" \
    "--data-dir=$DATA_DIR" \
    "--listen-client-urls=http://127.0.0.1:$CLIENT_PORT" \
    "--advertise-client-urls=http://127.0.0.1:$CLIENT_PORT" \
    "--listen-peer-urls=http://127.0.0.1:$PEER_PORT" \
    "--initial-advertise-peer-urls=http://127.0.0.1:$PEER_PORT" \
    "--initial-cluster=$ETCD_NAME=http://127.0.0.1:$PEER_PORT" \
    "--listen-metrics-urls=http://127.0.0.1:$METRICS_PORT" \
    "--quota-backend-bytes=$QUOTA_BYTES" \
    "--log-level=info" \
    "--logger=zap" \
    "--log-outputs=$logfile"
}

wait_health() {
  local deadline=$((SECONDS + ${1:-$HEALTH_TIMEOUT_S}))
  while ((SECONDS < deadline)); do
    if curl -fsS --max-time 5 "$(client_endpoint)/health" 2>/dev/null | grep -q '"health":"true"'; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# Pairs for a scenario id (S3a-ref -> PAIRS_S3a_ref). SMOKE forces 1.
pairs_for() {
  if [[ -n "${SMOKE:-}" ]]; then echo 1; return; fi
  local var="PAIRS_${1//-/_}"
  printf '%s' "${!var:?no pair count for $1}"
}

group_for() {
  local var="GROUP_$1"
  printf '%s' "${!var:?unknown group $1}"
}

# Fault hooks only act in smoke mode (PLAN P5b), so they can never fire in real runs.
fault_is() { [[ -n "${SMOKE:-}" && "${FAULT:-}" == "$1" ]]; }
