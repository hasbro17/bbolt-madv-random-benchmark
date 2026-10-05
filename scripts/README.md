# Scripts

Everything used to run the benchmark and build the report. **To reproduce the runs, follow
[`../docs/REPRODUCE.md`](../docs/REPRODUCE.md)**; this page only says what each file is for.

The scripts expect a Linux 6.4+ host with cgroup v2 and systemd, run as root, from
`/opt/bench/scripts` (results go to `/opt/bench/results/`). Comments in the scripts refer to
the phases in `REPRODUCE.md` as "PLAN P2" to "PLAN P7", and to the five benchmark hosts as
VM A to E (one scenario group each, see `config.env`).

## Entry points, in the order they run

| Step | Script | Does |
|---|---|---|
| Host | `host-setup.sh` | Installs packages and Go, mounts the data disk at `/data`, turns swap off, splits CPUs between etcd and the client, quiets background timers, records a preflight |
| Build | `build.sh` | Builds control and treatment etcd from one etcd checkout; treatment only adds a `go.work` replace of bbolt |
| | `verify-variants.sh` | Starts each build and checks the database mapping: control must show the `rr` (`MADV_RANDOM`) VmFlag, treatment must not |
| Datasets | `make-golden.sh` | Builds the 8.6 GB "history" dataset (and the compacted copy) with the control build |
| Memory limit | `calibrate.sh`, `p5-calibrate.sh` | Runs the S3a compaction at several memory limits to choose one; `p5-calibrate.sh` runs the whole sweep unattended |
| Scenarios | `run-matrix.sh` | Runs one host's scenario group pair by pair, alternating control and treatment; resumable |
| Supplementary | `ra128-d.sh` | The S2 follow-up at 128 KiB readahead (edit the data device name first) |
| | `s4-recheck.sh` | The S4 drift check: reruns the first 8 workload mixes with control after the treatment sweep |
| | `mglru.sh` | Switches MGLRU off or on for the MGLRU-off arm |

## What one run does

- `run-one.sh`: one run of one scenario for one build. Copies the dataset, drops caches,
  starts etcd, applies the memory limit once etcd is healthy, checks the build (binary
  checksum and the `rr` flag), runs the scenario's client commands, and writes `run.json`.
- `start-etcd.sh`, `stop-etcd.sh`: start etcd as a transient systemd unit with an optional
  `MemoryMax`, and stop it.
- `collect.py`: 1 Hz sampler for etcd's `/proc` counters, its cgroup's `memory.stat` and the
  data disk.
- `vmflags.py`: reads the VmFlags of etcd's database mapping (the build check above).
- `postcheck.py`: flags a run whose data disk sat at its throughput limit the whole time.
- `validate_s4.py`: rejects an `rw-benchmark.sh` CSV with empty or zero cells.
- `lib.sh`, `runjson.py`: shared shell helpers and a small `run.json` editor.
- `config.env`: shared settings: pairs per scenario, scenario groups per host, ports, paths.

## `scenarios/`

One file per scenario (`S1.sh` ... `S4.sh`, `S1-long.sh`, the `-ref` runs without a memory
limit, `S3a-mglru-off.sh`), plus `_common.sh` (shared helpers) and `mini.sh` (a tiny smoke
scenario). These files and `config.env` are byte-identical to what ran: their hash is
recorded as `scenario_rev` in every run's `run.json` in the raw data, and `scenario_rev()` in
`run-matrix.sh` recomputes it from the files here.

## `report/`

Turns `results/` into the report, the HTML page and the summary comment:
`check_complete.py` (every pair present and consistent), `aggregate.py` (per-run metrics and
per-pair ratios into `aggregate.json`), `supplementary.py` (the follow-up and side
measurements), `render.py` (all text, tables and charts; needs matplotlib). `tests/` and
`testdata/` are its unit tests and fixtures. Commands: `REPRODUCE.md`, section "Report".

## Other files

- `bbolt-remove-madv-random.patch`: the treatment, applied to bbolt v1.5.0 (see
  [`../docs/VERSIONS.md`](../docs/VERSIONS.md)).
- `local/`, `tests/`: checks run before the cloud runs: the client commands against a local
  etcd (`run-local.sh`), the Linux-only guards in a container (`podman-test.sh`), and the
  `run-matrix.sh` logic with a stand-in runner (`test-run-matrix.sh`).
- The host-provisioning and result-copying helpers tied to the original cloud setup are
  left out; any Linux 6.4+ host set up as in `REPRODUCE.md` works.
