# Benchmark scenarios, in plain terms

## The setup

- **Two etcd builds**, identical except one line in bbolt:
  - **control**: bbolt as shipped (`MADV_RANDOM` on)
  - **treatment**: that line removed (the fix)
- **The bug needs three things at once:** a kernel 6.4+, memory pressure (etcd's cached
  pages get evicted), and compaction (which reads lots of those evicted pages back).
- **How we create memory pressure:** limit etcd's memory below its database size, so the
  kernel has to keep evicting etcd's pages.

## The scenarios

| | What it does | Why | Expect |
|---|---|---|---|
| **S1** Reads, plenty of memory | Point reads and range reads at several client counts | Does the fix hurt normal reads? | Same |
| **S2** Reads, memory limited | Same reads, but under the memory limit | The real risk: without `MADV_RANDOM`, each page fault also reads neighbouring pages, which could waste cache | Same or better |
| **S3a** One big compaction, memory limited | Load a DB full of old revisions, compact it once, time it | The headline: same work for both builds, one clean number | Control slow, fix fast |
| **S3b** Steady writes + compaction every minute, memory limited | Writes and periodic compactions, with a background reader churning the cache | Closer to a busy production cluster | Control slow, fix fast |
| **S4** etcd's `rw-benchmark.sh` | etcd's own read/write throughput sweep, unmodified | ahrtr asked for this tool by name | Same |

**S3a and S3b without the memory limit** are also run as references. With nothing evicted,
both builds should compact equally fast, which shows the limit is what triggers the bug.

## Extra checks

| | What it does | Why |
|---|---|---|
| **Gate** (before the main runs) | Confirm control really is slow under the limit | If we can't reproduce the bug, a "no difference" result means nothing |
| **MGLRU off** | Rerun S3a with the kernel's newer page-reclaim mode switched off | Tests the explanation for the bug from #939: is MGLRU really the trigger? |
| **S1-long** | S1 with 10x the requests per point-read step | S1's point-read steps last only 0.5-4 s |
| **S2 at a 25% limit** | S2 with the cache smaller than the reads' working set, at 4 MiB and at 128 KiB readahead | The one case where readahead could waste cache; it turns out to depend on the readahead size |
| **S4 drift check** | Rerun 8 S4 cells with control after the treatment sweep | S4's two sweeps run hours apart; rules out drift on the VM |

## What "success" looks like

- S1, S4: **no meaningful difference**; S2: **same or better** (the fix doesn't hurt anything)
- S3a, S3b: **fix is much faster** under memory pressure (the fix helps where it matters)

That is exactly ahrtr's condition for just removing the line: *no impact other than
compaction.*
