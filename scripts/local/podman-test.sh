#!/usr/bin/env bash
# shellcheck disable=SC2015 # pass() never fails, so A && pass || fail is safe
# P0 step 9: Linux-only pieces in a UBI 10 container on a real Linux kernel:
# the rr variant guard (and so the patch itself), the pre-run guards with their failure
# cases, and collect.py against a running etcd. systemd-run, real memory pressure and
# EBS stay for the P3 mini smoke on VM A.
#
# Usage: podman-test.sh            (runs on the Mac, drives the container)
#        podman-test.sh --inside   (what runs inside the container)
set -euo pipefail
SCRIPTS="$(cd "$(dirname "$0")/.." && pwd)"
BASE="$(cd "$SCRIPTS/.." && pwd)"
IMAGE=${IMAGE:-registry.access.redhat.com/ubi10/ubi:latest}

if [[ ${1:-} != --inside ]]; then
  build="$BASE/local/build-linux-arm64"
  [[ -x $build/control/etcd ]] || { echo "missing $build; run P0 step 4 first" >&2; exit 1; }
  out="$BASE/local/podman-out"
  rm -rf "$out"
  mkdir -p "$out"
  exec podman run --rm --network none \
    -v "$SCRIPTS:/opt/bench/scripts:ro,Z" \
    -v "$build:/opt/variants:ro,Z" \
    -v "$out:/out:Z" \
    --tmpfs /data:rw,size=2g \
    "$IMAGE" bash /opt/bench/scripts/local/podman-test.sh --inside
fi

# ---------------------------------------------------------------- inside the container
dnf -q -y install python3 procps-ng util-linux curl-minimal findutils >/dev/null 2>&1 || true
export MODE=linux BENCH_ROOT=/work VARIANTS_ROOT=/opt/variants GOLDEN_ROOT=/work/golden
export DATA_MOUNT=/data DATA_DIR=/data/etcd ENV_DIR=/work/env STATE_DIR=/work/state RESULTS_ROOT=/out/results
export MIN_FREE_DATA_GIB=1 MIN_FREE_ROOT_GIB=1
mkdir -p "$ENV_DIR" "$STATE_DIR" "$GOLDEN_ROOT"
cp /opt/variants/versions.env "$ENV_DIR/versions.env"
S=/opt/bench/scripts
fails=0
pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; fails=$((fails + 1)); }
rj() { python3 "$S/runjson.py" get "$1/run.json" "$2"; }
run() { # run SCENARIO VARIANT DIR [env...] -> sets $rc
  local scen=$1 v=$2 d=$3; shift 3
  rc=0
  env "$@" MINI_READS=3000 "$S/run-one.sh" "$scen" "$v" "$d" >"$d.log" 2>&1 || rc=$?
}

"$S/make-golden.sh" --name mini --keys 3000 --passes 2 --clients 8 --conns 2 >/out/golden.log 2>&1 \
  || { echo "golden build failed"; tail -20 /out/golden.log; exit 1; }

R=/out/results/mini
mkdir -p "$R"

# Variant guard, positive cases: the patch removes rr from the db mapping.
run mini control "$R/control"
[[ $rc == 0 && $(rj "$R/control" db_has_rr) == True ]] && pass "control runs and its db mapping has rr" \
  || fail "control (rc=$rc rr=$(rj "$R/control" db_has_rr) reason=$(rj "$R/control" reason))"
run mini treatment "$R/treatment"
[[ $rc == 0 && $(rj "$R/treatment" db_has_rr) == False ]] && pass "treatment runs and its db mapping has no rr" \
  || fail "treatment (rc=$rc rr=$(rj "$R/treatment" db_has_rr) reason=$(rj "$R/treatment" reason))"
echo "control db VmFlags:   $(rj "$R/control" db_vmflags)"
echo "treatment db VmFlags: $(rj "$R/treatment" db_vmflags)"

# Collector produced samples with real counters.
python3 - "$R/control/samples.csv" <<'PY' && pass "collector wrote samples with process and cgroup counters" || fail "collector samples"
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
assert len(rows) >= 5, f"only {len(rows)} samples"
last = rows[-1]
for k in ("majflt", "minflt", "utime_ticks", "rss_bytes"):
    assert last[k] not in ("", None), f"{k} empty"
assert int(last["minflt"]) > 0
print(f"  samples={len(rows)} cg_memory_current={last['cg_memory_current']!r} io_read_bytes={last['io_read_bytes']!r}")
PY

# Variant guard, negative case: the control binary started for a treatment run.
run mini treatment "$R/wrong-binary" SMOKE=1 FAULT=wrong-binary
[[ $rc == 20 && $(rj "$R/wrong-binary" reason) == *"variant guard"* ]] && pass "wrong binary is fatal (variant guard)" \
  || fail "wrong binary (rc=$rc reason=$(rj "$R/wrong-binary" reason))"

# Checksum guard.
run mini control "$R/bad-sha" SMOKE=1 FAULT=bad-sha
[[ $rc == 20 && $(rj "$R/bad-sha" reason) == *checksum* ]] && pass "bad checksum is fatal before etcd starts" \
  || fail "bad sha (rc=$rc reason=$(rj "$R/bad-sha" reason))"
[[ -z $(rj "$R/bad-sha" etcd_pid) ]] && pass "etcd never started on bad checksum" || fail "etcd started despite bad checksum"

# Fault hooks are inert without SMOKE.
run mini control "$R/fault-no-smoke" FAULT=bad-sha
[[ $rc == 0 ]] && pass "FAULT ignored without SMOKE" || fail "FAULT fired without SMOKE (rc=$rc)"

# Mount guard: DATA_MOUNT that is not a mount point.
mkdir -p /notamount
run mini control "$R/no-mount" DATA_MOUNT=/notamount DATA_DIR=/notamount/etcd
[[ $rc == 11 && $(rj "$R/no-mount" reason) == *"not mounted"* ]] && pass "unmounted data dir is invalid" \
  || fail "mount guard (rc=$rc reason=$(rj "$R/no-mount" reason))"

# Disk space guard.
run mini control "$R/low-disk" MIN_FREE_DATA_GIB=100000
[[ $rc == 11 && $(rj "$R/low-disk" reason) == *"low disk"* ]] && pass "low disk is invalid" \
  || fail "disk guard (rc=$rc reason=$(rj "$R/low-disk" reason))"

# Benchmark error detection: a failing client step fails the run.
run mini control "$R/bench-error" SMOKE=1 FAULT=bench-error-always
[[ $rc == 10 ]] && pass "client failure fails the run" || fail "bench error (rc=$rc)"

# postcheck: synthetic saturated and unsaturated disk samples.
python3 - <<'PY' && pass "postcheck flags a saturated volume only" || fail "postcheck"
import subprocess, tempfile, os
def write(rate_mib):
    fd, p = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    with open(p, "w") as f:
        f.write("ts,disk_sectors_read,disk_sectors_written\n")
        s = 0
        for i in range(60):
            f.write(f"{i},{s},0\n"); s += int(rate_mib * 2**20 / 512)
    return p
S = "/opt/bench/scripts/postcheck.py"
assert subprocess.run(["python3", S, write(125), "--max-mibps", "125"]).returncode == 1
assert subprocess.run(["python3", S, write(40), "--max-mibps", "125"]).returncode == 0
PY

echo "failures: $fails"
exit "$fails"
