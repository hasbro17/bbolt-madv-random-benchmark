# Method, decisions and reruns

How the runs were done, every change of method during the experiment and why, and every
failed or rerun attempt. The raw data for all of it, including failed attempts, is in the
release tarball.

## Design

- **Two builds, one difference.** etcd `main` with bbolt v1.5.0 as shipped (control) and
  the same with the `MADV_RANDOM` call removed (treatment). See [VERSIONS.md](VERSIONS.md).
- **Pairs.** Every comparison is a control run and a treatment run back to back on the same
  VM. Odd pairs run control first, even pairs treatment first. The statistic is the
  per-pair ratio treatment / control, summarised as the median and range over pairs.
- **Same VM only.** Each scenario ran on one VM (five identical VMs ran the scenario
  groups in parallel). Absolute numbers are never compared across VMs; where two VMs are
  involved (the readahead follow-up) only ratios are compared.
- **Fresh state per run.** Stop etcd, delete the data dir, copy the same dataset
  ([DATASET.md](DATASET.md)), drop caches, start etcd, wait for health, apply the memory
  limit if the scenario has one ([PRESSURE.md](PRESSURE.md)).
- **Guards on every run.** Binary checksum, `/data` mounted and enough free space, MGLRU
  state as the scenario expects, and the `rr` VmFlag on the db mapping (present for
  control, absent for treatment). A failed guard stops the run before it counts.
- **Isolation.** Dedicated-tenancy VMs; etcd and the benchmark client pinned to separate
  physical cores (4 each, both hyperthreads); background timers (fstrim, dnf makecache,
  sysstat, insights) disabled; swap off.
- **Collection.** A 1 Hz sampler reads `/proc/<pid>` (faults, I/O), the cgroup's
  `memory.stat` and `/proc/diskstats`; `/metrics` is snapshotted before and after; the
  compaction time is etcd's own `finished scheduled compaction` log line.
- **Retries.** A failed run reruns its whole pair (up to 2 retries); failed attempts are
  kept, never deleted, under `_failed/`.

## Method changes and decisions, in order

1. **Random point reads use `benchmark stm`**, not `benchmark range`: `range` reads one
   fixed key or prefix and `txn-mixed` reads start at the beginning of the keyspace, so
   neither touches random pages. `stm --keys=<n> --keys-per-txn=1 --txn-wr-percent=0` reads
   one random existing key per transaction. `stm` uses 16-byte varint keys, so the
   datasets use 16-byte keys.
2. **The memory limit is applied after etcd is healthy**, not from the start: with the limit set from
   the start, control took 46.6 min to become healthy (index rebuild faulting page by
   page; treatment 5.7 min), which would have dominated every run. Startup under a limit is
   reported separately.
3. **Readahead left at the OS default of the image**: the RHEL 10 tuned profile
   `virtual-guest` sets `read_ahead_kb=4096` on the data volume. Kept as is (a stock host);
   a 128 KiB arm was added later (item 9).
4. **S4 sweep reduced to 60 cells** (5 read:write ratios x 256 B, 1 KiB, 4 KiB values x
   4 client counts; the 512 B and 2 KiB sizes dropped): real cells take about 6-7 minutes
   (mostly the script's own single-client 65,536-key load per cell), so 100 cells would
   have taken over 10 hours per variant. A first, partial
   100-cell attempt was stopped and kept under `S4/_failed/`.
5. **Latencies of rate-limited `stm` steps are not used** (S2's 10-minute mixed pass,
   S3b's background reader): `tools/benchmark/cmd/stm.go` calls the rate limiter inside
   the timed transaction, so with `--rate` the recorded latency is mostly the limiter's
   wait (clients / rate: 64 / 2,000 = 32 ms for both builds). Their throughput is kept.
   `put` and `range` start the clock after the limiter, so their latencies stand.
6. **S3b is judged on the mean compaction time per run plus major faults**, not the
   median: control's damage is one stalled compaction per run (~238 s, the first after the
   memory limit is applied) that a median hides. Decided after 2 of 5 S3b pairs, before the other 3 and the reference ran.
7. **S1-long added**: S1's point-read steps are short (0.5, 1.2, 4.3 s), so S1 was
   repeated with 10x the requests per point-read step (about 4, 11, 42 s), 5 pairs.
8. **S2 at a 25% limit added**: in the main S2 run the treatment's working set fits after
   the limit is applied (it never reclaimed again: 0 pages scanned), so the case "cache
   smaller than the working set" never arose. The same S2 was run at 2.27 GiB, 3 pairs.
9. **Readahead arm added**: at the 25% limit the treatment regressed on large reads with
   the 4 MiB readahead, so the same S2 was run with `read_ahead_kb=128` (the kernel
   default) on another VM, 3 pairs, restored to 4096 afterwards.
10. **S4 drift check added**: S4 runs the whole control sweep, then the whole treatment
    sweep about 7 hours later, so the first 8 cells were rerun with control afterwards.
11. **Dose-response cut to one round** (S3a at 60/40/25% limits) to free its VM for S1-long.

## Failed and superseded attempts

- **S4:** the partial 100-cell control sweep (item 4), shelved when the sweep was reduced.
- Every other scenario and supplementary run completed all pairs on the first attempt:
  no failed or invalid runs, no superseded pairs.
- Calibration (choosing the limit) had two stopped attempts, kept under
  `_calibration-aborted/` and `_calibration-cap-at-start/`: a guard that compared the limit
  before page rounding, and the limited-from-start runs that led to item 2.
- Before the main runs, smoke tests exercised every failure path on purpose (interrupted
  run, reboot mid-run, client error, retry limit, wrong binary, bad checksum, VM
  replacement, restore from backup); those outputs are under `_smoke*/` in the tarball and
  never reach the report.

## Fixes to the tooling during the experiment

None changed what was measured:

- The systemd unit could not write its log under `/opt` with SELinux enforcing; the script
  now writes its own log (found in smoke tests, before the main runs).
- A JSON helper typed `infinity` as a float and could drop a leading zero from an
  all-digit hash; fixed mid-run (a fresh process per call, so running work was unaffected;
  no stored value was affected).
- The report pipeline briefly could pick a stale CSV left by the shelved S4 attempt in a
  local copy; it now takes the newest and warns, and every local file was checked against
  the VMs before reporting.

## Scope

Linux 6.4+ only (tested on 6.12, RHEL 10). Nothing here speaks to older kernels: no
pre-6.4 arm was run.
