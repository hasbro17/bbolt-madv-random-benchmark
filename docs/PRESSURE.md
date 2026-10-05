# Memory pressure

Pressure comes from capping etcd's cgroup below its database size, so the kernel must
reclaim etcd's own page cache (the mmap'd db is charged to that cgroup):

```
systemd-run --unit=etcd-bench -p MemoryMax=<cap> -p MemorySwapMax=0 taskset -c <etcd cpus> etcd ...
```

The limit is applied **after etcd is healthy** (`systemctl set-property --runtime ... MemoryMax=<cap>`),
which models a running etcd squeezed by memory pressure. Capping from the start also makes
startup slow (index rebuild faulting page by page); that is measured separately in the
report ("More results").

## Choosing the limit

Calibrated on one VM with S3a (one cold compaction of the 8.6 GB history dataset):

| Setting | Control compaction | Notes |
|---|--:|---|
| No limit | 21.5 s | 0 major faults; treatment identical |
| anon + 60% of the db = **5,466,398,720 bytes (5.09 GiB)** | 1,123 s | 1,479,464 major faults, 5,987 MiB read during compaction |

Baseline after startup without a limit: anon ~269 MiB, page cache ~8.5 GB (the whole db).
The 60% point was the largest limit tried and already slowed control 52x, so it became the
limit for every memory-limited scenario. The limit is rounded down to a 4 KiB page because the kernel
stores `memory.max` page-rounded.

## Validity gate

Before the main runs, each VM that runs memory-limited scenarios had to reproduce the bug at that
limit: one control/treatment pair of S3a.

| VM | Control | Treatment |
|---|--:|--:|
| A | 1,123 s (the calibration run) | 65.2 s, 65.1 s (2 runs) |
| B | 771 s | 65.2 s |
| C | 1,076 s | 65.2 s |

Absolute control times differ between hosts, which is why every comparison in the report
is within one VM.

## Other limits used

- **25% (2,442,752,000 bytes, 2.27 GiB):** below the reads' working set; used for the
  "memory below the working set" follow-up and the dose-response.
- **40% (3,738,599,424 bytes, 3.48 GiB):** dose-response and the limited-startup measurement.

`MemorySwapMax=0` and swap off on every host: reclaim can only drop page cache, never swap.
