#!/usr/bin/env python3
"""Collect the supplementary measurements that are not part of the paired scenarios.

  supplementary.py RESULTS_DIR [--out RESULTS_DIR/supplementary.json]

- startup: etcd start -> healthy time with the memory cap applied from the start
  (results/_calibration/startup-capped-at-start/<variant>-N/), plus the uncapped startup
  of every no-cap reference run for comparison.
- dose: S3a compaction at several caps on one host (results/_dose/_calibration/dose-F/).
- calibration: the P5 runs that chose the cap (results/_calibration/{nocap,cap-*}/).
- s1_long: S1 with 10x longer point-read steps (results/_s1-long/), H1 robustness.
- s2_tight: S2 at the 25% cap with the stock 4 MiB readahead (results/_s2-cap25/, VM C)
  and with read_ahead_kb=128 (results/_s2-cap25-ra128/, VM D): boltdb/bolt#383's case.
- s4_recheck: 8 control cells rerun after the S4 treatment sweep (results/_s4-recheck/),
  compared with both S4 sweeps, to bound drift on E.
Each entry keeps the run directory so numbers can be traced back.
"""
import argparse
import glob
import json
import os
import statistics

import aggregate


def marks_span(rundir, a, b):
    m = aggregate.load_marks(os.path.join(rundir, "marks.csv"))
    return (m[b] - m[a]) if a in m and b in m else None


def runs(pattern):
    for rd in sorted(glob.glob(pattern)):
        if os.path.exists(os.path.join(rd, "run.json")):
            rj = json.load(open(os.path.join(rd, "run.json")))
            yield rd, rj


def med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--out")
    a = ap.parse_args()
    out = {"startup": {}, "dose": {}, "calibration": []}

    for rd, rj in runs(os.path.join(a.root, "_calibration", "startup-capped-at-start", "*")):
        v = rj.get("variant")
        out["startup"].setdefault(v, []).append({
            "run": os.path.relpath(rd, a.root), "vm": rj.get("vm"), "cap": rj.get("cap"),
            "startup_s": marks_span(rd, "etcd-start", "etcd-healthy"), "status": rj.get("status")})
    uncapped = [marks_span(rd, "etcd-start", "etcd-healthy")
                for rd, rj in runs(os.path.join(a.root, "*", "pair-*", "*"))
                if rj.get("cap") in ("infinity", float("inf"))]
    out["startup_uncapped_median_s"] = med(uncapped)

    for d in sorted(glob.glob(os.path.join(a.root, "_dose", "_calibration", "dose-*"))):
        label = os.path.basename(d)
        entry = {}
        for rd, rj in runs(os.path.join(d, "*")):
            if rj.get("status") != "ok":
                continue
            _, m = aggregate.run_metrics(rd, "S3a")
            entry.setdefault(rj["variant"], []).append({
                "run": os.path.relpath(rd, a.root), "vm": rj.get("vm"), "cap": rj.get("cap"),
                "compaction_s": m.get("compaction_s"), "compaction_majflt": m.get("compaction_majflt"),
                "compaction_read_mib": m.get("compaction_read_mib")})
        for v in list(entry):
            entry[v + "_median_s"] = med([r["compaction_s"] for r in entry[v]])
        out["dose"][label] = entry

    for rd, rj in runs(os.path.join(a.root, "_calibration", "*", "*")):
        if "startup" in rd:
            continue
        _, m = aggregate.run_metrics(rd, "S3a")
        out["calibration"].append({
            "run": os.path.relpath(rd, a.root), "vm": rj.get("vm"), "variant": rj.get("variant"),
            "cap": rj.get("cap"), "status": rj.get("status"), "compaction_s": m.get("compaction_s"),
            "compaction_majflt": m.get("compaction_majflt"), "compaction_read_mib": m.get("compaction_read_mib")})

    def scen_summary(root, scen, keys):
        if not os.path.isdir(root):
            return None
        s = aggregate.aggregate(root)["scenarios"].get(scen)
        if not s or not s["n_pairs"]:
            return None
        return {"n_pairs": s["n_pairs"], "vms": s["vms"], "caps": s.get("caps"),
                "metrics": {k: v for k, v in s["summary"].items() if k in keys}}

    s1_keys = [f"stm-c{c}.{m}" for c in (16, 64, 256) for m in ("p99_ms", "rps")]
    out["s1_long"] = scen_summary(os.path.join(a.root, "_s1-long"), "S1-long", s1_keys)
    s2_keys = s1_keys + [f"range500-{c}-c{n}.p99_ms" for c in "ls" for n in (16, 64, 256)] + [
        "list-paginate.avg_ms", "mixed-range500.p99_ms", "majflt_total", "read_mib_total"]
    out["s2_tight"] = {ra: scen_summary(os.path.join(a.root, d), "S2", s2_keys)
                       for ra, d in (("4096", "_s2-cap25"), ("128", "_s2-cap25-ra128"))}

    rc = sorted(glob.glob(os.path.join(a.root, "_s4-recheck", "control-1", "result-*.csv")))
    s4 = sorted(glob.glob(os.path.join(a.root, "S4", "pair-01", "control", "result-*.csv")))
    s4t = sorted(glob.glob(os.path.join(a.root, "S4", "pair-01", "treatment", "result-*.csv")))
    early = sorted(glob.glob(os.path.join(a.root, "S4", "_failed", "*", "control", "result-*.csv")))
    if rc and s4:
        sweeps = {"control_recheck": aggregate.parse_s4_csv(rc[-1]), "control": aggregate.parse_s4_csv(s4[-1])}
        if s4t:
            sweeps["treatment"] = aggregate.parse_s4_csv(s4t[-1])
        if early:
            sweeps["control_early_partial"] = aggregate.parse_s4_csv(early[-1])
        cells = sorted(sweeps["control_recheck"])
        out["s4_recheck"] = {"cells": {c: {k: v.get(c) for k, v in sweeps.items()} for c in cells}}
    else:
        out["s4_recheck"] = None

    path = a.out or os.path.join(a.root, "supplementary.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1, default=str)
    print(f"startup: {json.dumps({k: [r['startup_s'] for r in v] for k, v in out['startup'].items()})}"
          f" uncapped median {out['startup_uncapped_median_s']}")
    dose = ", ".join("%s: c=%s t=%s" % (k, v.get("control_median_s"), v.get("treatment_median_s"))
                     for k, v in out["dose"].items())
    print(f"dose: {dose}")
    print(f"calibration runs: {len(out['calibration'])}; wrote {path}")


if __name__ == "__main__":
    main()
