# Reproducing the runs

Everything runs on Linux 6.4+ with cgroup v2 and systemd (tested on RHEL 10, kernel 6.12).
The scripts expect to live at `/opt/bench/scripts` and run as root. Script comments refer to
the phases below as "PLAN Pn".

## Host

- A VM or machine with at least 8 physical cores (etcd and the client get 4 whole cores
  each) and enough RAM to cache the whole database when without a memory limit (64 GiB used).
- A separate data disk for `/data`. Network block storage behaves like the original
  environment (gp3: 3000 IOPS, 125 MiB/s); fast local NVMe hides much of the page-fault
  cost.
- `scripts/host-setup.sh <data volume id>` (P2): installs packages and Go, formats and
  mounts `/data`, turns swap off, derives the CPU split into `env/cpus.env`, disables
  background timers that do disk or CPU work, installs the `bench-matrix` systemd unit and
  records a preflight (`uname`, MGLRU state, cgroup version, readahead, THP). Check
  `read_ahead_kb` on the data disk: results under tight memory depend on it (see the
  report's readahead section).

## Build (P3)

```
scripts/build.sh --etcd-src <etcd checkout at the pinned commit> \
  --bbolt-treatment <bbolt v1.5.0 with scripts/bbolt-remove-madv-random.patch applied> \
  --out /opt/variants
scripts/verify-variants.sh     # control's db mapping must show VmFlag rr, treatment's must not
```

Versions and checksums: [VERSIONS.md](VERSIONS.md).

## Datasets (P4)

```
scripts/make-golden.sh --name history --keys 350000 --passes 4 --compacted-name compacted
```

The benchmark writes random values, so a rebuilt dataset has the same shape but different
bytes and checksums. Details: [DATASET.md](DATASET.md).

## Memory limit (P5)

```
scripts/calibrate.sh nocap control none
scripts/calibrate.sh cap60 control <anon + 60% of the db, in bytes>
scripts/calibrate.sh cap60 treatment <same>
scripts/calibrate.sh --summary
```

Write the chosen limit to `/opt/bench/env/cap.env` as `CAP_BYTES=...`. Rationale and the
values used: [PRESSURE.md](PRESSURE.md).

## Scenarios (P6)

```
scripts/run-matrix.sh A        # or B..E: one scenario group per host (scripts/config.env GROUP_*)
SMOKE=1 scripts/run-matrix.sh A    # short smoke pass first
```

| Group | Scenarios |
|---|---|
| A | S3a, S3a-ref, S3a-mglru-off |
| B | S3b, S3b-ref |
| C | S2 |
| D | S1 |
| E | S4 (needs the etcd source tree for `tools/rw-heatmaps/rw-benchmark.sh`) |

Results land in `/opt/bench/results/<scenario>/pair-NN/{control,treatment}/`. The matrix is
resumable: rerunning it skips finished pairs. Supplementary runs used the same scripts:

- S1-long: `RESULTS_ROOT=/opt/bench/results/_s1-long PAIRS_S1_long=5 run-matrix.sh --scenarios S1-long`
- S2 at the 25% limit: `RESULTS_ROOT=/opt/bench/results/_s2-cap25 CAP_OVERRIDE=<bytes> PAIRS_S2=3 run-matrix.sh --scenarios S2`
- Readahead arm: `scripts/ra128-d.sh` (same as above into `_s2-cap25-ra128/`, with `read_ahead_kb=128`
  on the data disk for the run; edit the device name in the script)
- S4 drift check: `scripts/s4-recheck.sh /opt/bench/results/_s4-recheck/control-1` after S4
- Dose-response: `RESULTS_ROOT=/opt/bench/results/_dose calibrate.sh dose-<fraction> <variant> <cap>`;
  startup under a limit: `CAP_AT=start` with `calibrate.sh`

`supplementary.py` reads these directory names.

## Report (P7)

```
python3 scripts/report/check_complete.py results --config scripts/config.env \
  --versions <versions.env> --dataset <dataset.env> --cap <cap.env> \
  --scenarios "S3a S3a-ref S3a-mglru-off S3b S3b-ref S2 S1 S4"
python3 scripts/report/aggregate.py results
python3 scripts/report/supplementary.py results
python3 scripts/report/render.py results/aggregate.json --out . --supp results/supplementary.json --publish
```

`render.py` needs matplotlib. Every number in the report, the HTML page and the summary
comment comes from `aggregate.json` and `supplementary.json`.
