#!/usr/bin/env python3
"""Flag a run whose data volume sat at its throughput ceiling for the whole run.

  postcheck.py samples.csv --max-mibps 125

Exit 1 if at least 95% of samples (and at least 30 of them) ran at >= 97% of the
volume's throughput limit: the run measured the EBS ceiling, not etcd. Prints a summary.
"""
import argparse
import csv
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("samples")
    ap.add_argument("--max-mibps", type=float, required=True)
    a = ap.parse_args()
    rows = []
    with open(a.samples) as f:
        for r in csv.DictReader(f):
            try:
                rows.append((float(r["ts"]), int(r["disk_sectors_read"]) + int(r["disk_sectors_written"])))
            except (KeyError, ValueError):
                continue
    rates = []
    for (t0, s0), (t1, s1) in zip(rows, rows[1:]):
        if t1 > t0:
            rates.append((s1 - s0) * 512 / (t1 - t0) / 2**20)
    if len(rates) < 30:
        print(f"samples={len(rates)} (too few to judge)")
        return 0
    hot = sum(1 for r in rates if r >= 0.97 * a.max_mibps)
    frac = hot / len(rates)
    print(f"samples={len(rates)} at_limit={hot} fraction={frac:.3f} mean_mibps={sum(rates)/len(rates):.1f}")
    return 1 if frac >= 0.95 else 0


if __name__ == "__main__":
    sys.exit(main())
