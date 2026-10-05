#!/usr/bin/env python3
"""Aggregate raw benchmark results into one JSON file both reports read (PLAN P7).

  aggregate.py RESULTS_DIR [--out RESULTS_DIR/aggregate.json] [--include-smoke]

For every scenario directory (S3a, S3a-ref, ...) and every completed pair (pair.done with
both runs ok) it extracts per-run metrics from the raw files, computes the per-pair ratio
treatment/control for every metric both runs have, and summarises each metric across pairs
(control and treatment medians, median/min/max of the pair ratios). The per-pair ratio is
the primary statistic (SPEC 4.2.2): it is measured within one host, back to back.

Sources per run: run.json, marks.csv, etcd.log (compaction lines), bench-*.txt (benchmark
reports), samples.csv (1 Hz collector), metrics-{start,end}.txt (Prometheus), and for S4
the rw-benchmark.sh result CSV.
"""
import argparse
import csv
import datetime as dt
import glob
import json
import math
import os
import re
import statistics
import sys

SCENARIO_ORDER = ["S3a", "S3a-ref", "S3a-mglru-off", "S3b", "S3b-ref", "S2", "S1", "S4", "mini"]

# --------------------------------------------------------------------------- parsers

_DUR = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
_UNIT = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}


def go_duration(s):
    """Parse a Go duration string ("1.41s", "922.8ms", "1m2.5s") into seconds."""
    parts = _DUR.findall(s or "")
    if not parts:
        return None
    return sum(float(v) * _UNIT[u] for v, u in parts)


def parse_bench(path):
    """Parse an etcd `benchmark` report (Summary + Latency distribution)."""
    try:
        text = open(path, errors="replace").read()
    except OSError:
        return None
    out = {}
    m = re.search(r"Requests/sec:\s+([\d.]+)", text)
    if m:
        out["rps"] = float(m.group(1))
    for key, label in (("total_s", "Total"), ("slowest_s", "Slowest"), ("avg_s", "Average")):
        m = re.search(label + r":\s+([\d.]+) secs", text)
        if m:
            out[key] = float(m.group(1))
    for pct, val in re.findall(r"^\s+([\d.]+)% in ([\d.]+) secs", text, re.M):
        name = {"50": "p50", "90": "p90", "99": "p99", "99.9": "p999"}.get(pct)
        if name:
            out[name + "_ms"] = float(val) * 1000.0
    # Few-request steps (e.g. a 2-request full list) print no percentiles; average and
    # slowest request time are the metrics there.
    if "avg_s" in out:
        out["avg_ms"] = out["avg_s"] * 1000.0
    if "slowest_s" in out:
        out["slowest_ms"] = out["slowest_s"] * 1000.0
    out["errors"] = "Error distribution" in text
    return out if "rps" in out else None


def zap_ts(s):
    """zap timestamps look like 2026-10-02T16:08:21.548-0700; return epoch seconds."""
    if isinstance(s, (int, float)):
        return float(s)
    s = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", s.replace("Z", "+00:00"))
    try:
        return dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def parse_compactions(path):
    out = []
    try:
        lines = open(path, errors="replace").read().splitlines()
    except OSError:
        return out
    for line in lines:
        if "finished scheduled compaction" not in line:
            continue
        try:
            j = json.loads(line)
        except ValueError:
            continue
        took = go_duration(j.get("took", ""))
        end = zap_ts(j.get("ts"))
        out.append({
            "took_s": took,
            "keys": j.get("number-of-keys-compacted"),
            "rev": j.get("compact-revision"),
            "end_ts": end,
            "start_ts": end - took if (end is not None and took is not None) else None,
            "db_bytes": j.get("current-db-size-bytes"),
        })
    return out


def load_marks(path):
    marks = {}
    try:
        for line in open(path):
            ts, _, label = line.strip().partition(",")
            if label and label not in marks:
                marks[label] = float(ts)
    except OSError:
        pass
    return marks


def load_samples(path):
    rows = []
    try:
        with open(path) as f:
            for r in csv.DictReader(f):
                row = {}
                for k, v in r.items():
                    try:
                        row[k] = float(v)
                    except (TypeError, ValueError):
                        row[k] = None
                if row.get("ts") is not None:
                    rows.append(row)
    except OSError:
        pass
    return rows


def at(rows, t, field):
    """Value of a cumulative counter at time t (last sample at or before t)."""
    best = None
    for r in rows:
        if r["ts"] <= t and r.get(field) is not None:
            best = r[field]
        elif r["ts"] > t:
            break
    if best is None:
        for r in rows:
            if r.get(field) is not None:
                return r[field]
    return best


def delta(rows, t0, t1, field):
    a, b = at(rows, t0, field), at(rows, t1, field)
    if a is None or b is None:
        return None
    return b - a


def prom_hist(path, name):
    """Return {le: cumulative count} for a Prometheus histogram in a text exposition."""
    out = {}
    try:
        for line in open(path):
            if line.startswith(name + "_bucket{"):
                le = re.search(r'le="([^"]+)"', line).group(1)
                out[float("inf") if le == "+Inf" else float(le)] = float(line.rsplit(" ", 1)[1])
    except OSError:
        pass
    return out


def hist_quantile(start, end, q):
    """Quantile from the delta of two cumulative histograms (upper bucket bound)."""
    if not end:
        return None
    d = {le: end[le] - start.get(le, 0.0) for le in end}
    total = d.get(float("inf"))
    if not total:
        return None
    for le in sorted(d):
        if d[le] >= q * total:
            return le
    return None


def parse_s4_csv(path):
    cells = {}
    with open(path) as f:
        reader = csv.reader(f)
        header = next(reader)
        iters = [i for i, h in enumerate(header) if h.startswith("iter")]
        for r in reader:
            if not r or r[0] != "DATA":
                continue
            reads, writes = [], []
            for i in iters:
                p = r[i].split(":") if i < len(r) else []
                if len(p) == 2:
                    try:
                        reads.append(float(p[0]))
                        writes.append(float(p[1]))
                    except ValueError:
                        pass
            key = f"ratio={r[1]},conn={r[2]},value={r[3]}"
            cells[key] = {
                "read_qps": statistics.mean(reads) if reads else None,
                "write_qps": statistics.mean(writes) if writes else None,
            }
    return cells

# --------------------------------------------------------------------------- per run


def run_metrics(rundir, scenario):
    rj = json.load(open(os.path.join(rundir, "run.json")))
    m = {}
    marks = load_marks(os.path.join(rundir, "marks.csv"))
    rows = load_samples(os.path.join(rundir, "samples.csv"))
    t0 = marks.get("client-start", rj.get("start_ts"))
    t1 = marks.get("client-end", rj.get("end_ts"))

    if rows and t0 and t1:
        maj = delta(rows, t0, t1, "majflt")
        rd = delta(rows, t0, t1, "disk_sectors_read")
        ref = delta(rows, t0, t1, "cg_workingset_refault_file")
        span = max(t1 - t0, 1e-9)
        if maj is not None:
            m["majflt_total"] = maj
            m["majflt_per_s"] = maj / span
        if rd is not None:
            m["read_mib_total"] = rd * 512 / 2**20
            m["read_mib_per_s"] = m["read_mib_total"] / span
        if ref is not None:
            m["refault_file_total"] = ref
        mem = [r["cg_memory_current"] for r in rows if r.get("cg_memory_current") is not None]
        if mem:
            m["cg_memory_peak_mib"] = max(mem) / 2**20

    comps = parse_compactions(os.path.join(rundir, "etcd.log"))
    if comps:
        tooks = [c["took_s"] for c in comps if c["took_s"] is not None]
        m["compactions"] = len(comps)
        if scenario.startswith("S3a") or scenario == "mini":
            c = comps[-1]
            m["compaction_s"] = c["took_s"]
            m["compaction_keys"] = c["keys"]
            ws, we = marks.get("compact-start"), marks.get("compact-end")
            if rows and ws and we:
                maj = delta(rows, ws, we, "majflt")
                rd = delta(rows, ws, we, "disk_sectors_read")
                if maj is not None:
                    m["compaction_majflt"] = maj
                if rd is not None:
                    m["compaction_read_mib"] = rd * 512 / 2**20
        elif tooks:
            m["compaction_s_median"] = statistics.median(tooks)
            m["compaction_s_max"] = max(tooks)
            m["compaction_s_mean"] = statistics.mean(tooks)

    for path in sorted(glob.glob(os.path.join(rundir, "bench-*.txt"))):
        step = os.path.basename(path)[len("bench-"):-len(".txt")]
        b = parse_bench(path)
        if not b:
            continue
        for k in ("p50_ms", "p99_ms", "p999_ms", "rps", "avg_ms", "slowest_ms"):
            if k in b:
                m[f"{step}.{k}"] = b[k]

    ms, me = os.path.join(rundir, "metrics-start.txt"), os.path.join(rundir, "metrics-end.txt")
    for short, name in (("backend_commit", "etcd_disk_backend_commit_duration_seconds"),
                        ("wal_fsync", "etcd_disk_wal_fsync_duration_seconds")):
        q = hist_quantile(prom_hist(ms, name), prom_hist(me, name), 0.99)
        if q is not None and math.isfinite(q):
            m[f"{short}_p99_ms"] = q * 1000.0

    if scenario == "S4":
        # The local mirror never deletes, so a run dir can keep the CSV of an attempt the VM
        # later shelved. rw-benchmark.sh names CSVs by start time: the newest is this run's.
        csvs = sorted(glob.glob(os.path.join(rundir, "result-*.csv")))
        if len(csvs) > 1:
            print(f"warning: {rundir}: {len(csvs)} result CSVs, using {os.path.basename(csvs[-1])}", file=sys.stderr)
        if csvs:
            m["_cells"] = parse_s4_csv(csvs[-1])

    info = {k: rj.get(k) for k in ("status", "vm", "binary_sha", "cap", "golden", "scenario_rev",
                                    "tooling_rev", "db_vmflags", "db_has_rr", "kernel", "mglru",
                                    "restore_s", "hostname")}
    return info, m

# --------------------------------------------------------------------------- summary


def summarise(values):
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if not vals:
        return None
    return {"median": statistics.median(vals), "min": min(vals), "max": max(vals), "n": len(vals)}


def scenario_dirs(root, include_smoke):
    out = {}
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        name = os.path.basename(d)
        if os.path.isdir(d) and not name.startswith((".", "_")):
            out[name] = d
    if include_smoke:
        for d in sorted(glob.glob(os.path.join(root, "_smoke", "*"))):
            name = os.path.basename(d)
            if os.path.isdir(d) and not name.startswith((".", "_")):
                out.setdefault(name, d)
    return out


def aggregate(root, include_smoke=False):
    result = {"generated_ts": int(dt.datetime.now().timestamp()), "root": os.path.abspath(root), "scenarios": {}}
    dirs = scenario_dirs(root, include_smoke)
    for scen in sorted(dirs, key=lambda s: (SCENARIO_ORDER.index(s) if s in SCENARIO_ORDER else 99, s)):
        pairs = []
        for pdir in sorted(glob.glob(os.path.join(dirs[scen], "pair-*"))):
            done = os.path.join(pdir, "pair.done")
            if not os.path.exists(done):
                continue
            pd = json.load(open(done))
            entry = {"pair": os.path.basename(pdir), "order": pd.get("order"), "attempts": pd.get("attempts"),
                     "scenario_rev": pd.get("scenario_rev")}
            ok = True
            for v in ("control", "treatment"):
                rd = os.path.join(pdir, v)
                if not os.path.exists(os.path.join(rd, "run.json")):
                    ok = False
                    break
                info, metrics = run_metrics(rd, scen)
                if info.get("status") != "ok":
                    ok = False
                entry[v] = {"info": info, "metrics": metrics}
            if not ok:
                continue
            c, t = entry["control"]["metrics"], entry["treatment"]["metrics"]
            entry["ratio"] = {k: t[k] / c[k] for k in c
                              if k in t and not k.startswith("_") and isinstance(c[k], (int, float))
                              and isinstance(t[k], (int, float)) and c[k]}
            if "_cells" in c and "_cells" in t:
                cells = {}
                for key in c["_cells"]:
                    a, b = c["_cells"][key], t["_cells"].get(key, {})
                    cells[key] = {f: (b.get(f) / a[f]) if a.get(f) and b.get(f) is not None else None
                                  for f in ("read_qps", "write_qps")}
                entry["cell_ratio"] = cells
            pairs.append(entry)

        summary = {}
        keys = sorted({k for p in pairs for k in p["ratio"]})
        for k in keys:
            summary[k] = {
                "control": summarise([p["control"]["metrics"].get(k) for p in pairs]),
                "treatment": summarise([p["treatment"]["metrics"].get(k) for p in pairs]),
                "ratio": summarise([p["ratio"].get(k) for p in pairs]),
                "ratios": [p["ratio"].get(k) for p in pairs],
            }
        if any("cell_ratio" in p for p in pairs):
            for f in ("read_qps", "write_qps"):
                vals = [r[f] for p in pairs for r in p.get("cell_ratio", {}).values() if r.get(f)]
                summary[f"cells.{f}"] = {"ratio": summarise(vals), "ratios": vals}
        vms = sorted({p[v]["info"].get("vm") for p in pairs for v in ("control", "treatment")} - {None})
        caps = sorted({str(p[v]["info"].get("cap")) for p in pairs for v in ("control", "treatment")})
        result["scenarios"][scen] = {"pairs": pairs, "summary": summary, "n_pairs": len(pairs), "vms": vms,
                                     "caps": caps}
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--out")
    ap.add_argument("--include-smoke", action="store_true")
    a = ap.parse_args()
    res = aggregate(a.root, a.include_smoke)
    out = a.out or os.path.join(a.root, "aggregate.json")
    with open(out, "w") as f:
        json.dump(res, f, indent=1, sort_keys=True, default=str)
    for scen, s in res["scenarios"].items():
        print(f"{scen}: {s['n_pairs']} pairs, {len(s['summary'])} metrics, vms={s['vms']}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
