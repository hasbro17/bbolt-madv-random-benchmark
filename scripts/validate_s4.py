#!/usr/bin/env python3
"""Validate an rw-benchmark.sh result CSV.

rw-benchmark.sh sends the benchmark's stderr to /dev/null and writes 0 when a run
fails, so a broken cell looks like a slow one. Every DATA row must have its iteration
cells as "READ_QPS:WRITE_QPS" with both values present and greater than zero.

  validate_s4.py result-YYYYMMDDHHMM.csv      exit 1 on any bad cell
"""
import csv
import sys


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    bad, rows = [], 0
    with open(sys.argv[1]) as f:
        reader = csv.reader(f)
        header = next(reader)
        iters = [i for i, h in enumerate(header) if h.startswith("iter")]
        for r in reader:
            if not r or r[0] != "DATA":
                continue
            rows += 1
            cell_id = f"ratio={r[1]} conn={r[2]} value={r[3]}"
            for i in iters:
                v = r[i] if i < len(r) else ""
                parts = v.split(":")
                try:
                    ok = len(parts) == 2 and all(float(p) > 0 for p in parts)
                except ValueError:
                    ok = False
                if not ok:
                    bad.append(f"{cell_id} {header[i]}={v!r}")
    print(f"data rows={rows} bad cells={len(bad)}")
    for b in bad:
        print("BAD", b)
    if rows == 0 or bad:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
