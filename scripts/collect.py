#!/usr/bin/env python3
"""1 Hz sampler for one etcd process, its cgroup and the data block device.

  collect.py --pid PID --out FILE [--cgroup DIR] [--dev NAME] [--interval 1]

Writes a CSV with cumulative counters; rates are computed later by aggregate.py.
Runs until SIGTERM/SIGINT or until the process disappears. Missing sources are left
blank rather than failing, so the same script works on a VM, in a container and in
tests. Block device stats come from /proc/diskstats (the same counters iostat uses).
"""
import argparse
import os
import signal
import sys
import time

FIELDS = [
    "ts", "majflt", "minflt", "utime_ticks", "stime_ticks", "rss_bytes",
    "io_read_bytes", "io_write_bytes",
    "cg_memory_current", "cg_anon", "cg_file", "cg_pgmajfault",
    "cg_workingset_refault_file", "cg_pgscan", "cg_pgsteal", "cg_oom_kill",
    "disk_reads", "disk_sectors_read", "disk_writes", "disk_sectors_written", "disk_io_ms",
]
PAGE = os.sysconf("SC_PAGE_SIZE")
running = True


def stop(*_):
    global running
    running = False


def read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def proc_stat(pid):
    raw = read(f"/proc/{pid}/stat")
    if raw is None:
        return None
    # Fields after the ")" closing comm; index 0 is field 3 (state).
    rest = raw.rsplit(")", 1)[1].split()
    return {
        "minflt": rest[7], "majflt": rest[9],
        "utime_ticks": rest[11], "stime_ticks": rest[12],
        "rss_bytes": str(int(rest[21]) * PAGE),
    }


def kv_file(path):
    raw = read(path)
    out = {}
    if raw:
        for line in raw.splitlines():
            parts = line.replace(":", " ").split()
            if len(parts) >= 2:
                out[parts[0]] = parts[1]
    return out


def diskstats(dev):
    raw = read("/proc/diskstats") if dev else None
    if not raw:
        return {}
    for line in raw.splitlines():
        p = line.split()
        if len(p) >= 13 and p[2] == dev:
            return {
                "disk_reads": p[3], "disk_sectors_read": p[5],
                "disk_writes": p[7], "disk_sectors_written": p[9], "disk_io_ms": p[12],
            }
    return {}


def sample(pid, cg, dev):
    st = proc_stat(pid)
    if st is None:
        return None
    row = {"ts": f"{time.time():.3f}", **st}
    io = kv_file(f"/proc/{pid}/io")
    row["io_read_bytes"] = io.get("read_bytes", "")
    row["io_write_bytes"] = io.get("write_bytes", "")
    if cg:
        row["cg_memory_current"] = (read(f"{cg}/memory.current") or "").strip()
        ms = kv_file(f"{cg}/memory.stat")
        for k in ("anon", "file", "pgmajfault", "workingset_refault_file", "pgscan", "pgsteal"):
            row[f"cg_{k}"] = ms.get(k, "")
        row["cg_oom_kill"] = kv_file(f"{cg}/memory.events").get("oom_kill", "")
    row.update(diskstats(dev))
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cgroup", default="")
    ap.add_argument("--dev", default="")
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with open(a.out, "w", buffering=1) as f:
        f.write(",".join(FIELDS) + "\n")
        nxt = time.monotonic()
        while running:
            row = sample(a.pid, a.cgroup, a.dev)
            if row is None:
                break
            f.write(",".join(str(row.get(k, "")) for k in FIELDS) + "\n")
            nxt += a.interval
            time.sleep(max(0.0, nxt - time.monotonic()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
