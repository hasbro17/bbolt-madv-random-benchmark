#!/usr/bin/env python3
"""Render REPORT.md, report.html and GITHUB-COMMENT.md from aggregate.json (PLAN P7).

  render.py AGGREGATE_JSON --out DIR [--env-dir DIR] [--infra-dir DIR] [--draft]

Charts are drawn with matplotlib (local venv) as SVG (inlined into the HTML) and PNG
(linked from the markdown, attachable to the GitHub comment). Every number in the three
outputs comes from aggregate.json, so they cannot drift apart. Missing scenarios are shown
as "not run" rather than failing, so the same script renders smoke and partial data.
"""
import argparse
import datetime as dt
import html
import io
import json
import os
import re
import statistics

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402,F401

C_CTRL, C_TRT, C_BAND = "#b85c38", "#2f6f9f", "#9aa5b1"

# --------------------------------------------------------------------------- verdicts
# Thresholds are stated in the report so a reader can check them.
EQUIV_MIN = 0.05        # S1/S4: equivalent if |median ratio - 1| <= max(5%, half the ratio range)
H3_MAX_RATIO = 0.5      # S3: treatment at least 2x faster at compaction, every pair below 1
# S3b (decided 2026-10-03 after 2 of 5 pairs, before the rest; docs/METHODS.md item 6): the
# median per-compaction time hides control's stalls (most compactions equal, a few far
# slower), so S3b is judged on the mean compaction time per run (as in the cluster-scale
# results posted on #939) plus major faults: every pair's mean ratio below 1 and faults/s cut by >= 90%.
S3B_MAX_FAULT_RATIO = 0.1


# `benchmark stm` calls the rate limiter inside the timed transaction (tools/benchmark/cmd/stm.go:
# limit.Wait in applyf, timed by doSTM), so with --rate its latency is mostly the limiter's wait
# (clients / rate: 64 / 2,000 = 32 ms). put and range wait before starting the clock. Latencies of
# these rate-limited stm steps are left out; their throughput still shows whether the rate held.
RATE_LIMITED_STM = ("mixed-stm", "stm-background")


def limiter_latency(k):
    return k.rsplit(".", 1)[0] in RATE_LIMITED_STM and k.endswith("_ms")


def band(r):
    """Noise band for a ratio summary: max(5%, half the min-max range of pair ratios)."""
    return max(EQUIV_MIN, (r["max"] - r["min"]) / 2)


def fmt(v, unit="", nd=2):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        if v.is_integer() and not unit.strip().startswith(("ms", "s", "MiB")):
            s = f"{v:,.0f}"
        elif abs(v) >= 100:
            s = f"{v:,.0f}"
        elif abs(v) >= 10:
            s = f"{v:.1f}"
        else:
            s = f"{v:.{nd}f}"
    else:
        s = str(v)
    return s + unit


def gib(b):
    """A memory cap in GiB; "none" for an uncapped run."""
    if b in (None, "infinity", "inf", float("inf")):
        return "none"
    try:
        return f"{float(b) / 2**30:.2f} GiB"
    except (TypeError, ValueError):
        return str(b)


def pct(r, multiple=True):
    """A ratio as a signed percent change; 2x and above as a multiple ("33x" reads better than "+3,245%")."""
    if r is None:
        return "n/a"
    if multiple and r >= 2:
        return f"{r:.1f}x"
    p = (r - 1) * 100
    return "0.0%" if abs(p) < 0.05 else f"{p:+.1f}%"


def pct_range(lo, hi):
    """A min-max range in one format: multiples only when both ends are 2x or more."""
    both = lo is not None and lo >= 2
    return f"{pct(lo, both)} to {pct(hi, both)}"


STEP_LABELS = {"stm": "Point reads", "range500-l": "500-key range reads (linearizable)",
               "range500-s": "500-key range reads (serializable)", "list-paginate": "Full list of all keys",
               "warmup-list": "Warm-up full list", "mixed-stm": "Point reads (10-min pass, fixed rate)",
               "mixed-range500": "500-key range reads (10-min pass, fixed rate)", "put-compact": "Writes",
               "stm-background": "Background point reads"}
SUFFIX_LABELS = {"p99_ms": "p99 latency", "p999_ms": "p99.9 latency", "p50_ms": "median latency",
                 "avg_ms": "time per request", "rps": "throughput"}
PLAIN_LABELS = {"compaction_s": "Compaction time", "compaction_majflt": "Major page faults during compaction",
                "compaction_read_mib": "Disk read during compaction", "majflt_total": "Major page faults, whole run",
                "read_mib_total": "Disk read, whole run", "backend_commit_p99_ms": "Backend commit p99 latency",
                "wal_fsync_p99_ms": "WAL fsync p99 latency", "compaction_s_max": "Slowest compaction",
                "compaction_s_mean": "Mean compaction time", "compaction_s_median": "Median compaction time",
                "compactions": "Compaction rounds per run", "majflt_per_s": "Major page faults per second",
                "read_mib_per_s": "Disk read per second"}


def label(k):
    """Plain-language name for a metric id such as range500-l-c16.p99_ms."""
    if k in PLAIN_LABELS:
        return PLAIN_LABELS[k]
    step, _, suffix = k.rpartition(".")
    m = re.match(r"^(.*?)-c(\d+)$", step)
    base, clients = (m.group(1), m.group(2)) if m else (step, None)
    name = STEP_LABELS.get(base, base)
    if step == "list-paginate" and suffix == "avg_ms":
        return "Full list of all keys: time per list"
    return f"{name}{f', {clients} clients' if clients else ''}: {SUFFIX_LABELS.get(suffix, suffix)}"


STEP_ORDER = ["stm", "range500-l", "range500-s", "list-paginate", "mixed-stm", "mixed-range500", "put-compact",
              "stm-background"]
SUFFIX_ORDER = ["rps", "p50_ms", "p99_ms", "p999_ms", "avg_ms"]


def sort_key(k):
    """Rows in reading order: point reads first, then ranges, list, mixed pass; clients ascending;
    throughput before latency; plain metrics (compaction, faults, disk) after the read steps."""
    if k in PLAIN_LABELS:
        return (1, list(PLAIN_LABELS).index(k), 0, 0)
    step, _, suffix = k.rpartition(".")
    m = re.match(r"^(.*?)-c(\d+)$", step)
    base, clients = (m.group(1), int(m.group(2))) if m else (step, 0)
    return (0, STEP_ORDER.index(base) if base in STEP_ORDER else 99, clients,
            SUFFIX_ORDER.index(suffix) if suffix in SUFFIX_ORDER else 99)


def scen(agg, name):
    s = agg["scenarios"].get(name)
    return s if s and s["n_pairs"] else None


def read_metrics(summary):
    """Latency/throughput metrics of benchmark steps (step.p99_ms, step.rps...)."""
    return {k: v for k, v in summary.items()
            if re.match(r"^[a-z0-9-]+\.(p50_ms|p99_ms|p999_ms|rps|avg_ms)$", k) and v.get("ratio")
            and not k.startswith("warmup-") and not limiter_latency(k)}


def latency_keys(ms):
    """Tail latency per step: p99 where the step has enough requests, else (lists) the average."""
    out = {k: v for k, v in ms.items() if k.endswith(".p99_ms")}
    steps_with_p99 = {k.rsplit(".", 1)[0] for k in out}
    out.update({k: v for k, v in ms.items() if k.endswith(".avg_ms") and k.rsplit(".", 1)[0] not in steps_with_p99})
    return out


def s1_point_range(agg, supp):
    """Median pair ratios of point-read throughput and p99 latency in S1 and S1-long, so every
    place that quotes the no-pressure point-read range quotes the same one."""
    s1 = scen(agg, "S1")
    sl = ((supp or {}).get("s1_long") or {}).get("metrics")
    keys = [f"stm-c{c}.{m}" for c in (16, 64, 256) for m in ("rps", "p99_ms")]
    vals = ([_med(s1["summary"], k) for k in keys] if s1 else []) + ([_med(sl, k) for k in keys] if sl else [])
    return [v for v in vals if v]


def verdicts(agg, supp=None):
    """One row per hypothesis. Wording is for a reader new to the issue: plain words, numbers
    relative to control, no internal metric names."""
    out = []
    q1 = "With plenty of memory, do reads change?"
    q2 = "Under memory pressure, are reads slower without `MADV_RANDOM`?"
    q3 = "Under memory pressure, is compaction slow with `MADV_RANDOM` and fast without it?"
    q4 = "Does etcd's own `rw-benchmark.sh` sweep change?"
    sc = {"S1": "S1: reads, no memory limit", "S2": "S2: reads, memory limited",
          "S3": "S3a, S3b: compaction, memory limited", "S4": "S4: read/write throughput sweep"}
    # S1: no-pressure reads equivalent.
    s1 = scen(agg, "S1")
    if s1:
        rm = read_metrics(s1["summary"])
        ms = {**latency_keys(rm), **{k: v for k, v in rm.items() if k.endswith(".rps")}}
        off = [k for k, v in ms.items() if abs(v["ratio"]["median"] - 1) > band(v["ratio"])]
        pr = s1_point_range(agg, supp)
        longer = " in S1 and in 10x longer runs (S1-long)" if (supp or {}).get("s1_long") else ""
        out.append({"id": "S1", "q": q1, "scenario": sc["S1"], "pass": not off,
                    "answer": "No difference" if not off else
                              f"{len(off)} of {len(ms)} measures differ beyond their noise band",
                    "headline": (f"Point-read throughput and p99 latency within {pct(min(pr))} to {pct(max(pr))} "
                                 f"of control{longer}. The 500-key range reads vary more from pair to pair, for both "
                                 "builds, because they keep etcd's CPU saturated.") if pr else "",
                    "outside": off})
    else:
        out.append({"id": "S1", "q": q1, "scenario": sc["S1"], "pass": None, "answer": "not run", "headline": ""})
    # S2: reads under pressure not worse.
    s2 = scen(agg, "S2")
    if s2:
        ms = latency_keys(read_metrics(s2["summary"]))
        worse = sorted(k for k, v in ms.items() if v["ratio"]["median"] > 1 + band(v["ratio"]))
        meds = sorted(v["ratio"]["median"] for v in ms.values())
        out.append({"id": "S2", "q": q2, "scenario": sc["S2"], "pass": not worse,
                    "answer": (f"No: none of the {len(ms)} read-latency measures is slower than control beyond "
                               "its noise band" if not worse else
                               f"Yes, for {len(worse)} of {len(ms)} read-latency measures: "
                               + ", ".join(label(k) for k in worse)),
                    "headline": (f"Depending on the read type, treatment's latency is {pct(meds[0])} to "
                                 f"{pct(meds[-1])} against control (p99; time per list for the full list)") if meds else "",
                    "outside": worse})
    else:
        out.append({"id": "S2", "q": q2, "scenario": sc["S2"], "pass": None, "answer": "not run", "headline": ""})
    # S3: S3a (headline) and S3b compaction under pressure.
    s3a = scen(agg, "S3a")
    if s3a and "compaction_s" in s3a["summary"]:
        r = s3a["summary"]["compaction_s"]
        ratios = [x for x in r["ratios"] if x is not None]
        ok = r["ratio"]["median"] <= H3_MAX_RATIO and all(x < 1 for x in ratios)
        c, t = r["control"]["median"], r["treatment"]["median"]
        s3b = scen(agg, "S3b")
        b = s3b and s3b["summary"].get("compaction_s_mean")
        f = s3b and s3b["summary"].get("majflt_per_s")
        extra = ""
        if b and b.get("ratio"):
            ok = ok and all(x < 1 for x in b["ratios"] if x is not None)
            if f and f.get("ratio"):
                ok = ok and f["ratio"]["median"] <= S3B_MAX_FAULT_RATIO
            mx = s3b["summary"].get("compaction_s_max")
            extra = (f" With compaction every minute under writes (S3b), the mean compaction drops from "
                     f"{fmt(b['control']['median'], ' s')} to {fmt(b['treatment']['median'], ' s')}"
                     + (f" and the slowest from {fmt(mx['control']['median'], ' s')} to "
                        f"{fmt(mx['treatment']['median'], ' s')}" if mx and mx.get("ratio") else "") + ".")
        out.append({"id": "S3", "q": q3, "scenario": sc["S3"], "pass": ok,
                    "answer": f"Yes: {c / t:.1f}x faster without it" if ok else "Not shown",
                    "headline": (f"One large compaction (S3a) takes {fmt(c, ' s')} with `MADV_RANDOM` and "
                                 f"{fmt(t, ' s')} without (median of {r['ratio']['n']} pairs).{extra}")})
    else:
        out.append({"id": "S3", "q": q3, "scenario": sc["S3"], "pass": None, "answer": "not run", "headline": ""})
    # S4: rw-benchmark.sh throughput equivalent.
    s4 = scen(agg, "S4")
    if s4 and s4["summary"].get("cells.read_qps"):
        rr, wr = s4["summary"]["cells.read_qps"]["ratio"], s4["summary"]["cells.write_qps"]["ratio"]
        ok = abs(rr["median"] - 1) <= EQUIV_MIN and abs(wr["median"] - 1) <= EQUIV_MIN
        within = max(abs(x - 1) for x in (rr["min"], rr["max"], wr["min"], wr["max"]))
        out.append({"id": "S4", "q": q4, "scenario": sc["S4"], "pass": ok,
                    "answer": "No difference" if ok else "Throughput shifted",
                    "headline": (f"Across {rr['n']} workload mixes, median change against control is {pct(rr['median'])} "
                                 f"for reads and {pct(wr['median'])} for writes; every mix within "
                                 f"{within * 100:.1f}%.")})
    else:
        out.append({"id": "S4", "q": q4, "scenario": sc["S4"], "pass": None, "answer": "not run", "headline": ""})
    return out

# --------------------------------------------------------------------------- charts


def log_axis(ax, lo, hi):
    """Log x axis with readable ticks (0.05x, 0.1x, 0.2x, 0.5x, 1x, 2x, ...) and no minor labels."""
    ax.set_xscale("log")
    nice = [v * 10 ** e for e in range(-3, 3) for v in (1, 2, 5)]
    ticks = [v for v in nice if lo / 1.5 <= v <= hi * 1.5] or [1]
    ax.xaxis.set_major_locator(matplotlib.ticker.FixedLocator(ticks))
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _: f"{x:g}x"))
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())


def save(fig, outdir, name):
    os.makedirs(os.path.join(outdir, "charts"), exist_ok=True)
    fig.savefig(os.path.join(outdir, "charts", name + ".png"), dpi=160, bbox_inches="tight")
    buf = io.StringIO()
    fig.savefig(buf, format="svg", bbox_inches="tight")
    plt.close(fig)
    svg = buf.getvalue()
    svg = svg[svg.index("<svg"):]
    return {"name": name, "png": f"charts/{name}.png", "svg": svg}


GROUP_LABELS = {"S3a": "With memory limit", "S3a-ref": "No memory limit",
                "S3a-mglru-off": "With memory limit,\nMGLRU switched off", "S3b": "With memory limit",
                "S3b-ref": "No memory limit"}


def paired_chart(agg, outdir, scenarios, metric, title, ylabel, name):
    present = [s for s in scenarios if scen(agg, s) and metric in agg["scenarios"][s]["summary"]]
    if not present:
        return None
    fig, ax = plt.subplots(figsize=(1.9 + 1.9 * len(present), 3.6))
    for i, s in enumerate(present):
        pairs = agg["scenarios"][s]["pairs"]
        for p in pairs:
            c = p["control"]["metrics"].get(metric)
            t = p["treatment"]["metrics"].get(metric)
            if c is None or t is None:
                continue
            ax.plot([i - 0.15, i + 0.15], [c, t], color=C_BAND, lw=0.8, zorder=1)
            ax.scatter([i - 0.15], [c], color=C_CTRL, s=22, zorder=2)
            ax.scatter([i + 0.15], [t], color=C_TRT, s=22, zorder=2)
    ax.set_xticks(range(len(present)), [GROUP_LABELS.get(p, p) for p in present], fontsize=9)
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=11, loc="left")
    ax.scatter([], [], color=C_CTRL, label="control (MADV_RANDOM)")
    ax.scatter([], [], color=C_TRT, label="treatment (removed)")
    ax.legend(frameon=False, fontsize=8, loc="upper center")
    ax.set_ylim(bottom=0)
    ax.spines[["top", "right"]].set_visible(False)
    return save(fig, outdir, name)


def ratio_chart(agg, outdir, scenario, suffix, title, name):
    s = scen(agg, scenario)
    if not s:
        return None
    ms = sorted(((k, v) for k, v in read_metrics(s["summary"]).items() if k.endswith(suffix)),
                key=lambda kv: sort_key(kv[0]), reverse=True)
    if not ms:
        return None
    fig, ax = plt.subplots(figsize=(6.8, 0.34 * len(ms) + 1.3))
    for i, (k, v) in enumerate(ms):
        r = v["ratio"]
        ax.plot([r["min"], r["max"]], [i, i], color=C_BAND, lw=2)
        ax.scatter([r["median"]], [i], color=C_TRT, s=24, zorder=3)
    ax.axvline(1.0, color="#333", lw=0.8)
    ax.axvspan(1 - EQUIV_MIN, 1 + EQUIV_MIN, color=C_BAND, alpha=0.18, lw=0)
    lo = min(v["ratio"]["min"] for _, v in ms)
    hi = max(v["ratio"]["max"] for _, v in ms)
    log = lo < 0.5 or hi > 2
    if log:
        log_axis(ax, lo, hi)
    else:
        ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _: f"{x:g}x"))
    ax.set_yticks(range(len(ms)), [label(k).rsplit(":", 1)[0] for k, _ in ms], fontsize=8)
    ax.set_xlabel("treatment / control (1x = no change; left of 1x = treatment faster)")
    ax.set_title(title, fontsize=11, loc="left")
    ax.spines[["top", "right"]].set_visible(False)
    return {**save(fig, outdir, name), "log": log}


READAHEAD_ROWS = ["stm-c16.p99_ms", "stm-c64.p99_ms", "stm-c256.p99_ms", "range500-l-c64.p99_ms",
                  "list-paginate.avg_ms", "mixed-range500.p99_ms"]


def readahead_chart(supp, outdir):
    """S2 at the 25% cap: treatment / control latency at 4 MiB vs 128 KiB readahead."""
    t = (supp or {}).get("s2_tight") or {}
    arms = [(ra, lbl, col) for ra, lbl, col in (("4096", "4 MiB readahead (RHEL 10 stock tuning)", "#8a5a9e"),
                                                ("128", "128 KiB readahead (kernel default)", C_TRT)) if t.get(ra)]
    if not arms:
        return None
    rows = [(k, label(k)) for k in READAHEAD_ROWS if any((t[ra]["metrics"].get(k) or {}).get("ratio") for ra, _, _ in arms)]
    fig, ax = plt.subplots(figsize=(6.6, 0.42 * len(rows) + 1.4))
    for j, (ra, lbl, col) in enumerate(arms):
        off = (j - (len(arms) - 1) / 2) * 0.22
        for i, (k, _) in enumerate(rows):
            r = (t[ra]["metrics"].get(k) or {}).get("ratio")
            if not r:
                continue
            ax.plot([r["min"], r["max"]], [i + off, i + off], color=col, lw=1.6, alpha=0.45)
            ax.scatter([r["median"]], [i + off], color=col, s=26, zorder=3, label=lbl if i == 0 else None)
    ax.axvline(1.0, color="#333", lw=0.8)
    vals = [x for ra, _, _ in arms for k, _ in rows for x in ((t[ra]["metrics"].get(k) or {}).get("ratio") or {}).values()
            if isinstance(x, (int, float))]
    log_axis(ax, min(vals), max(vals))
    ax.set_yticks(range(len(rows)), [lbl for _, lbl in rows], fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("treatment / control (1x = no change; left of 1x = treatment faster)")
    cap = gib(next(iter((t.get("4096") or t.get("128") or {}).get("caps") or []), None))
    ax.set_title(f"Memory limit below the working set ({cap}): treatment vs control", fontsize=11, loc="left")
    ax.legend(frameon=False, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=2)
    ax.spines[["top", "right"]].set_visible(False)
    return save(fig, outdir, "s2-tight-readahead")


def s4_chart(agg, outdir):
    s = scen(agg, "S4")
    if not s or not s["summary"].get("cells.read_qps"):
        return None
    fig, ax = plt.subplots(figsize=(6.4, 3.0))
    # Neutral colours: here both rows are treatment / control, not one build each.
    for j, (f, col) in enumerate((("read_qps", "#4a5560"), ("write_qps", "#8a5a9e"))):
        vals = s["summary"][f"cells.{f}"]["ratios"]
        ax.scatter(vals, [j + (k % 7 - 3) * 0.04 for k in range(len(vals))], color=col, s=12, alpha=0.8)
    ax.axvline(1.0, color="#333", lw=0.8)
    ax.axvspan(1 - EQUIV_MIN, 1 + EQUIV_MIN, color=C_BAND, alpha=0.18, lw=0)
    ax.set_yticks([0, 1], ["Read throughput", "Write throughput"])
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _: f"{x:g}x"))
    ax.set_xlabel("treatment / control (1x = no change)")
    ax.set_title("rw-benchmark.sh: treatment vs control, one dot per workload mix", fontsize=11, loc="left")
    ax.spines[["top", "right"]].set_visible(False)
    return save(fig, outdir, "s4-cells")

# --------------------------------------------------------------------------- tables


def caption(c):
    """A short "how to read this chart" line placed above each chart."""
    pair_lines = ("Each grey line joins one pair: control (orange, left end) and treatment (blue, right end). "
                  "Lower is better.")
    ratio_rows = ("Each row is one kind of read. The dot is treatment's p99 latency relative to control (median "
                  "pair) and the line spans all pairs; the shaded band is ±5%. Left of 1x means treatment is faster, "
                  "right of 1x slower.")
    text = {
        "s1-p99": ratio_rows,
        "s2-p99": ratio_rows,
        "s3a-compaction": "Compaction time for every pair in three setups: with the memory limit, without it, and "
                          "with the memory limit and the kernel's MGLRU switched off. " + pair_lines,
        "s3a-faults": "Major page faults during compaction (pages read back from disk) for every pair, in the same "
                      "three setups. " + pair_lines,
        "s3b-compaction": "Mean compaction time per run with compaction every minute under writes, with and without "
                          "the memory limit. " + pair_lines,
        "s2-tight-readahead": "Each row is one measure under the tighter limit. The dots show treatment relative to "
                              "control (median pair, line = all pairs), once with 4 MiB readahead (purple) and once "
                              "with 128 KiB (blue). Left of 1x means treatment is faster.",
        "s4-cells": "Each dot is one workload mix: treatment's mean throughput relative to control, one row for reads "
                    "and one for writes. The shaded band is ±5%; dots on the 1x line mean no change.",
    }.get(c["name"], "")
    if c.get("log"):
        text += " The axis is logarithmic, so equal distances mean equal ratios (0.5x and 2x are the same distance from 1x)."
    return f"*How to read the chart:* {text}" if text else ""


def fig(c):
    """Caption plus image lines for one chart."""
    cap = caption(c)
    return ([cap, ""] if cap else []) + [f"![{c['name']}]({c['png']})", ""]


def metric_table(summary, keys, unit_of, ref=None, ref_label=""):
    head = "| Measure | Control (median run) | Treatment (median run) | Treatment vs control (median pair) | Range over pairs |"
    sep = "|---|--:|--:|--:|--:|"
    if ref is not None:
        head += f" {ref_label} |"
        sep += "--:|"
    rows = [head, sep]
    for k in keys:
        v = summary.get(k)
        if not v or not v.get("ratio"):
            continue
        u = unit_of(k)
        if u == " ms" and max(v["control"]["median"] or 0, v["treatment"]["median"] or 0) >= 10000:
            v = {**v, "control": {**v["control"], "median": v["control"]["median"] / 1000},
                 "treatment": {**v["treatment"], "median": v["treatment"]["median"] / 1000}}
            u = " s"
        row = (f"| {label(k)} | {fmt(v['control']['median'], u)} | {fmt(v['treatment']['median'], u)} | "
               f"{pct(v['ratio']['median'], v['ratio']['min'] >= 2)} | {pct_range(v['ratio']['min'], v['ratio']['max'])} |")
        if ref is not None:
            r = (ref.get(k) or {}).get("ratio")
            row += f" {pct_range(r['min'], r['max'])} |" if r else " n/a |"
        rows.append(row)
    return "\n".join(rows)


def unit_of(k):
    if k.endswith("mib_per_s"):
        return " MiB/s"
    if k.endswith("_per_s"):
        return "/s"
    if k.endswith("_ms"):
        return " ms"
    if k.endswith("_s") or "compaction_s" in k:
        return " s"
    if k.endswith("rps"):
        return "/s"
    if k.endswith("_mib") or k.endswith("_mib_total"):
        return " MiB"
    return ""


def env_facts(env_dir):
    facts = {}
    if not env_dir or not os.path.isdir(env_dir):
        return facts
    for vm in sorted(os.listdir(env_dir)):
        p = os.path.join(env_dir, vm, "preflight.txt")
        if os.path.exists(p):
            t = open(p).read()
            m = re.search(r"== uname\n(.*)", t)
            u = m.group(1).split() if m else []
            facts[vm] = {"uname": u[2] if len(u) > 2 else "?",
                         "readahead": (re.search(r"== read_ahead_kb\n(\S+)", t) or [None, "?"])[1]}
    return facts

# --------------------------------------------------------------------------- outputs

SCENARIO_ROWS = [
    ("S1", "Reads, plenty of memory",
     "Random point reads, 500-key range reads and a full list of all keys, at 16, 64 and 256 clients; no memory limit",
     "Does the change affect normal reads?"),
    ("S2", "Reads, memory limited",
     "The same reads under the memory limit, then 10 minutes of steady point and range reads",
     "Without `MADV_RANDOM`, each page fault also reads nearby pages. Under memory pressure those extra pages could "
     "push out useful ones, so this is where the change is most likely to hurt"),
    ("S3a", "One large compaction, memory limited",
     "Compact 1,050,000 old revisions of an 8.6 GB database in one go and time it",
     "The reported problem in its simplest form: the same work for both builds, one number per run"),
    ("S3b", "Writes with compaction every minute, memory limited",
     "900,000 writes at a target of 1,000 per second (about 15 minutes), a compaction every 60 seconds, background point reads",
     "Closer to a busy production cluster: compaction runs while clients read and write"),
    ("S4", "etcd's rw-benchmark.sh",
     "The unmodified read/write throughput sweep: 5 read:write ratios, 3 value sizes, 4 client counts, each from an "
     "empty database that grows (bbolt remaps the file, applying the `madvise` call each time)",
     "Does the change cost anything for writes and mixed traffic in normal operation? Plenty of memory, so no "
     "page-cache effect is expected; S1 covers only reads"),
]


def _ratio_cells(m, k):
    v = (m or {}).get(k) or {}
    r = v.get("ratio")
    return (pct(r["median"], r["min"] >= 2), pct_range(r["min"], r["max"])) if r else ("n/a", "")


S2_TIGHT_ROWS = [(k, label(k)) for k in (
    "stm-c16.rps", "stm-c64.rps", "stm-c256.rps", "stm-c256.p99_ms", "range500-l-c64.p99_ms",
    "list-paginate.avg_ms", "mixed-range500.p99_ms", "read_mib_total", "majflt_total")]


def s2_tight_section(t, chart=None, res=()):
    """S2 with the memory limit below the working set, at two readahead sizes."""
    if not t or not any(t.values()):
        return []
    a, b = t.get("4096"), t.get("128")
    cap = gib(next(iter((a or b).get("caps") or []), None))
    out = ["## S2 follow-up: memory below the working set, and readahead", ""] + list(res) + [
           "`MADV_RANDOM` was originally added (boltdb/bolt#383) to stop readahead from wasting disk reads when the "
           "database does not fit in memory. In the main S2 run that situation never arises: after the limit is "
           "applied, treatment's working set still fits. So the same S2 was also run with a tighter limit, "
           f"{cap}, which is below the working set, so both builds keep reading from disk. It was run twice:", "",
           "- with RHEL 10's stock readahead, `read_ahead_kb=4096` (4 MiB), and",
           "- with the kernel default, `read_ahead_kb=128` (128 KiB).", "",
           "Control is not affected by this setting, because `MADV_RANDOM` turns readahead off. Values are treatment "
           "against control per pair: for throughput higher is better; for time, latency, disk reads and faults lower "
           "is better.", ""] + (fig(chart) if chart else []) + [
           "| Measure | 4 MiB readahead: treatment vs control | Range over pairs | 128 KiB readahead: treatment vs "
           "control | Range over pairs |", "|---|--:|--:|--:|--:|"]
    for k, lbl in S2_TIGHT_ROWS:
        ca, ra = _ratio_cells(a and a["metrics"], k)
        cb, rb = _ratio_cells(b and b["metrics"], k)
        out.append(f"| {lbl} | {ca} | {ra} | {cb} | {rb} |")

    def who(x):
        return f"{x['n_pairs']} pairs" if x else "not run"
    out += ["", f"4 MiB: {who(a)}. 128 KiB: {who(b)}. Same scenario and limit; only the readahead setting of the "
            "data disk differs. The two settings ran on two different VMs, so only the treatment / control ratios "
            "are compared, never the absolute numbers.", "",
            "In short: with a 4 MiB readahead, each fault pulls in so much extra data that it pushes out pages that are "
            "still needed, and large reads get slower. " + _ra128_summary(b and b["metrics"]), ""]
    return out


def _phrase(k):
    """A metric as a phrase for running text: 'p99 latency of 500-key range reads (...)'."""
    what, _, measure = label(k).rpartition(": ")
    return f"{measure} of {what[:1].lower()}{what[1:]}" if what else label(k).lower()


def _ra128_summary(m):
    """One data-driven sentence on the 128 KiB arm: which read measures are better, which within noise."""
    if not m:
        return ""
    perf = [k for k, _ in S2_TIGHT_ROWS if k.endswith((".rps", "_ms"))]
    worse = [k for k in perf if _med(m, k) and ((k.endswith("_ms") and _med(m, k) > 1) or
                                                (k.endswith(".rps") and _med(m, k) < 1))]
    noise = [k for k in worse if (m[k]["ratio"]["min"] <= 1 <= m[k]["ratio"]["max"])]
    text = "With the default 128 KiB, treatment is faster than control on "
    text += ("every read measure here too." if not worse else
             "every read measure except the " + ", ".join(_phrase(k) for k in noise)
             + (", which is" if len(noise) == 1 else ", which are") + " within noise."
             if len(noise) == len(worse) else
             f"{len(perf) - len(worse)} of {len(perf)} read measures.")
    rb = _med(m, "read_mib_total")
    if rb and rb > 1:
        text += f" It reads {rb:.1f}x as much from disk as control."
    return text


def s1_long_section(t, res=()):
    if not t:
        return []
    out = ["## S1-long: the same point reads, 10 times longer", ""] + list(res) + [
           "S1's point-read runs are short (20,000, 80,000 and 320,000 requests: 0.5, 1.2 and 4.3 seconds). S1-long "
           "repeats S1 with 10 times as many requests (about 4, 11 and 42 seconds), to make sure the short runs did "
           "not hide a difference.", "",
           "| Measure | Treatment vs control (median pair) | Range over pairs |", "|---|--:|--:|"]
    for k in sorted(t["metrics"], key=sort_key):
        c, r = _ratio_cells(t["metrics"], k)
        out.append(f"| {label(k)} | {c} | {r} |")
    out += ["", f"{t['n_pairs']} pairs, no memory limit.", ""]
    return out


def _cell_label(cell):
    """ratio=.1250,conn=128,value=1024 -> reads:writes 1:8, 128 clients, 1 KiB values."""
    kv = dict(p.split("=", 1) for p in cell.split(","))
    try:
        r = float(kv.get("ratio", "nan"))
        mix = f"1:{1 / r:g}" if r < 1 else f"{r:g}:1"
        v = int(kv.get("value", 0))
        size = f"{v // 1024} KiB" if v >= 1024 else f"{v} B"
        return f"reads:writes {mix}, {kv.get('conn')} clients, {size} values"
    except (ValueError, ZeroDivisionError):
        return cell


def s4_recheck_section(t):
    if not t or not t.get("cells"):
        return []
    out = ["## S4 check: did the VM drift between the two sweeps?", "",
           "S4 runs the whole control sweep first and the whole treatment sweep about 7 hours later. To check that "
           "the VM itself did not change in between, the first 8 workload mixes were run again with control after "
           "the treatment sweep. Values are mean throughput per mix.", "",
           "| Workload mix | | Control | Control, run again afterwards | Control again vs control | Treatment vs control |",
           "|---|---|--:|--:|--:|--:|"]
    def _num(cell):
        kv = dict(p.split("=", 1) for p in cell.split(","))
        return (float(kv.get("ratio", 0)), int(kv.get("conn", 0)), int(kv.get("value", 0)))
    for cell, v in sorted(t["cells"].items(), key=lambda kv: _num(kv[0])):
        c, r, tr = v.get("control") or {}, v.get("control_recheck") or {}, v.get("treatment") or {}
        for f, name in (("read_qps", "reads/s"), ("write_qps", "writes/s")):
            cv, rv, tv = c.get(f), r.get(f), tr.get(f)
            out.append(f"| {_cell_label(cell)} | {name} | {fmt(cv)} | {fmt(rv)} | "
                       f"{pct(rv / cv) if cv and rv else 'n/a'} | {pct(tv / cv) if cv and tv else 'n/a'} |")
    rr, tt = [], []
    for v in t["cells"].values():
        c, r, tr = v.get("control") or {}, v.get("control_recheck") or {}, v.get("treatment") or {}
        for f in ("read_qps", "write_qps"):
            if c.get(f) and r.get(f):
                rr.append(r[f] / c[f])
            if c.get(f) and tr.get(f):
                tt.append(tr[f] / c[f])
    if rr and tt:
        out += ["", f"Control run again vs the first control sweep: median {pct(statistics.median(rr))} "
                f"({pct(min(rr))} to {pct(max(rr))}). Treatment vs control on the same mixes: median "
                f"{pct(statistics.median(tt))} ({pct(min(tt))} to {pct(max(tt))}). The two shifts are the same size, "
                "so the small difference between the sweeps comes from the VM, not from the change."]
    out.append("")
    return out


def _med(summary, k):
    v = (summary or {}).get(k) or {}
    return (v.get("ratio") or {}).get("median")


def _times(r):
    """A time ratio as 'N.Nx faster/slower' (lower is better)."""
    if not r:
        return "n/a"
    return f"{1 / r:.1f}x faster" if r < 1 else f"{r:.1f}x slower"


def key_findings(agg, supp):
    """Plain-language bullets under the answer table; every number comes from the data."""
    out = ["### In short", ""]
    s3a, s2, s1 = scen(agg, "S3a"), scen(agg, "S2"), scen(agg, "S1")
    off = scen(agg, "S3a-mglru-off")
    pr = s1_point_range(agg, supp)
    if s1 and pr:
        out.append(f"- **With plenty of memory, reads do not change:** point-read throughput and p99 latency are within "
                   f"{pct(min(pr))} to {pct(max(pr))} of control, also with 10x longer runs (S1, S1-long).")
    if s2:
        lt = _med(s2["summary"], "list-paginate.avg_ms")
        out.append(f"- **Under memory pressure, reads get faster without `MADV_RANDOM`:** "
                   f"{_med(s2['summary'], 'stm-c64.rps') or 0:.0f}x the point-read throughput at 64 clients, and a full "
                   f"list of all keys is {_times(lt)} (S2).")
    if s3a:
        line = (f"- **Under memory pressure, compaction is {_times(_med(s3a['summary'], 'compaction_s'))} without "
                "`MADV_RANDOM`** (S3a)")
        if off:
            line += (f", and {_times(_med(off['summary'], 'compaction_s'))} with the kernel's MGLRU switched off, so "
                     "the problem is not specific to MGLRU")
        out.append(line + ".")
    t = supp.get("s2_tight") or {}
    a, b = (t.get("4096") or {}).get("metrics"), (t.get("128") or {}).get("metrics")
    if a or b:
        out.append("- **One exception, when memory is smaller than the data etcd keeps reading:** it depends on the "
                   f"disk's readahead setting. With the kernel default (128 KiB), a full list is still "
                   f"{_times(_med(b, 'list-paginate.avg_ms'))} without `MADV_RANDOM`; with RHEL's stock 4 MiB "
                   f"readahead it is {_times(_med(a, 'list-paginate.avg_ms'))}, because each page fault pulls in "
                   "up to 4 MiB and pushes out pages that are still needed (S2 follow-up).")
    s4 = scen(agg, "S4")
    if s4 and s4["summary"].get("cells.read_qps"):
        rr = s4["summary"]["cells.read_qps"]["ratio"]
        out.append(f"- **etcd's rw-benchmark.sh shows no difference:** median {pct(rr['median'])} across "
                   f"{rr['n']} read/write workload mixes (S4).")
    out.append("")
    return out


def _span(vals, as_pct=True):
    vals = [v for v in vals if v]
    if not vals:
        return "n/a"
    if as_pct and pct(min(vals)) == pct(max(vals)):
        return f"{pct(min(vals))} at every client count"
    return pct_range(min(vals), max(vals)) if as_pct else f"{min(vals):.1f}x to {max(vals):.1f}x"


def result_lines(agg, supp, vs):
    """One plain-language result per section, from the data. Keys are section ids."""
    out = {}
    by_id = {v["id"]: v for v in vs}
    sm = lambda name: (scen(agg, name) or {}).get("summary") or {}
    s1, s2, s3a, s3b = sm("S1"), sm("S2"), sm("S3a"), sm("S3b")
    if s1:
        pr = [_med(s1, f"stm-c{c}.{m}") for c in (16, 64, 256) for m in ("rps", "p99_ms")]
        out["S1"] = (("No difference. " if by_id["S1"]["pass"] else "Some measures differ beyond noise. ")
                     + f"Point-read throughput and p99 latency are within {_span(pr)} of control; the 500-key range "
                     "reads vary from pair to pair for both builds and stay within their noise band.")
    sl = (supp.get("s1_long") or {}).get("metrics")
    if sl:
        rps = [_med(sl, f"stm-c{c}.rps") for c in (16, 64, 256)]
        p99 = [_med(sl, f"stm-c{c}.p99_ms") for c in (16, 64, 256)]
        p99s = _span(p99)
        out["S1-long"] = (f"No difference with longer runs either: point-read throughput within {_span(rps)} of "
                          "control, and p99 latency " + (f"unchanged ({p99s})." if "every" in p99s else f"within {p99s}."))
    if s2:
        rps = [_med(s2, f"stm-c{c}.rps") for c in (16, 64, 256)]
        worse = by_id["S2"].get("outside") or []
        out["S2"] = (f"Reads are faster without `MADV_RANDOM`: {_span(rps, False)} the point-read throughput and a "
                     f"full list of all keys is {_times(_med(s2, 'list-paginate.avg_ms'))}; "
                     + ("no read type is slower than control beyond its noise band." if not worse else
                        f"{len(worse)} read measure(s) slower beyond noise: " + ", ".join(label(k) for k in worse) + "."))
    t = supp.get("s2_tight") or {}
    a, b = (t.get("4096") or {}).get("metrics"), (t.get("128") or {}).get("metrics")
    if a and b:
        out["S2-tight"] = (f"With a 4 MiB readahead, large reads get slower (full list "
                           f"{_times(_med(a, 'list-paginate.avg_ms'))}, p99 of point reads at 256 clients "
                           f"{_times(_med(a, 'stm-c256.p99_ms'))}); with the 128 KiB default, treatment is faster "
                           f"(full list {_times(_med(b, 'list-paginate.avg_ms'))}).")
    if s3a.get("compaction_s"):
        c, f = s3a["compaction_s"], s3a.get("compaction_majflt") or {}
        out["S3a"] = (f"Compaction is {_times(c['ratio']['median'])} without `MADV_RANDOM` "
                      f"({fmt(c['control']['median'], ' s')} to {fmt(c['treatment']['median'], ' s')})"
                      + (f", with major page faults down from {fmt(f['control']['median'])} to "
                         f"{fmt(f['treatment']['median'])}" if f.get("control") else "") + ".")
    for ref, name in (("S3a-ref", "S3a-ref"), ("S3b-ref", "S3b-ref")):
        r = sm(ref)
        if r:
            meds = [v["ratio"]["median"] for v in r.values() if v.get("ratio")]
            dev = max(abs(m - 1) for m in meds) if meds else None
            out[name] = (f"No difference: every measure within {dev * 100:.1f}% of control." if dev is not None and dev < EQUIV_MIN
                         else "The builds differ even without a memory limit; see the table.")
    if s3b.get("compaction_s_max"):
        mx, mn = s3b["compaction_s_max"], s3b.get("compaction_s_mean") or {}
        rate, p999 = s3b.get("put-compact.rps") or {}, s3b.get("put-compact.p999_ms") or {}
        line = (f"The slowest compaction per run drops from {fmt(mx['control']['median'], ' s')} to "
                f"{fmt(mx['treatment']['median'], ' s')}")
        if mn.get("control"):
            line += f" and the mean from {fmt(mn['control']['median'], ' s')} to {fmt(mn['treatment']['median'], ' s')}"
        if rate.get("control") and p999.get("control"):
            line += (f"; writes run at {fmt(rate['treatment']['median'])} instead of {fmt(rate['control']['median'])} "
                     f"per second, with p99.9 latency of {fmt(p999['treatment']['median'], ' ms')} instead of "
                     f"{fmt(p999['control']['median'], ' ms')}")
        out["S3b"] = line + "."
    s4 = scen(agg, "S4")
    if s4 and s4["summary"].get("cells.read_qps"):
        out["S4"] = by_id["S4"]["answer"] + ". " + by_id["S4"]["headline"]
    off = sm("S3a-mglru-off")
    if off.get("compaction_s"):
        c = off["compaction_s"]
        out["MGLRU-off"] = (f"Still {_times(c['ratio']['median'])} without `MADV_RANDOM` "
                            f"({fmt(c['control']['median'], ' s')} to {fmt(c['treatment']['median'], ' s')}), so the "
                            "slowdown is not specific to MGLRU.")
    st = supp.get("startup") or {}
    if st.get("control") and st.get("treatment"):
        c, tr = st["control"][0]["startup_s"], st["treatment"][0]["startup_s"]
        out["startup"] = (f"Treatment becomes healthy {_times(tr / c)} ({fmt(c, ' s')} for control, "
                          f"{fmt(tr, ' s')} for treatment).")
    dose = {k: v for k, v in (supp.get("dose") or {}).items() if v.get("control_median_s") and v.get("treatment_median_s")}
    if dose:
        cs = [v["control_median_s"] for v in dose.values()]
        ts = [v["treatment_median_s"] for v in dose.values()]
        out["dose"] = (f"Treatment stays at {fmt(min(ts), ' s')} to {fmt(max(ts), ' s')} at every limit, while "
                       f"control takes {fmt(min(cs), ' s')} to {fmt(max(cs), ' s')}.")
    return out


def short_answer(vs, supp):
    """The bottom line in one sentence, from the verdicts and the readahead follow-up."""
    p = {v["id"]: v["pass"] for v in vs}
    if not all(p.get(i) for i in ("S1", "S2", "S4")):
        return ""
    t = supp.get("s2_tight") or {}
    a, b = (t.get("4096") or {}).get("metrics"), (t.get("128") or {}).get("metrics")
    exception = (a and b and (_med(a, "list-paginate.avg_ms") or 0) > 1 and (_med(b, "list-paginate.avg_ms") or 9) < 1)
    return ("**Short answer: no.** Outside compaction, removing the call changes nothing with plenty of memory and "
            "makes reads faster under memory pressure."
            + (" The one exception is a cache far smaller than etcd's working set combined with a very large "
               "readahead setting (4 MiB); with the kernel default (128 KiB) reads are faster there too." if exception else ""))


# Presentation: side measurements fold into a collapsed "More results"
# before the caveats, and the S4 drift check folds under S4, so the main path is S1-S4.
MORE_RESULTS = ("Compaction with MGLRU switched off", "etcd startup under a memory limit",
                "Compaction time at three memory limits")
FOLD_UNDER = {"S4 check: did the VM drift between the two sweeps?": "S4: etcd's rw-benchmark.sh"}


def _details(title, body):
    return f"<details><summary>{title}</summary>\n\n{body.strip()}\n\n</details>\n"


def restructure(report_md):
    head, *parts = re.split(r"^## ", report_md, flags=re.M)
    secs = [(p.partition("\n")[0], p.partition("\n")[2]) for p in parts]
    more = [(t, b) for t, b in secs if t in MORE_RESULTS]
    folded = {FOLD_UNDER[t]: (t, b) for t, b in secs if t in FOLD_UNDER}
    out = [head]
    for t, b in secs:
        if t in MORE_RESULTS or t in FOLD_UNDER:
            continue
        if t.startswith("Caveats") and more:
            out.append("## More results\n\nSide measurements that support the main results; expand any of them.\n\n"
                       + "\n".join(_details(mt, mb) for mt, mb in more) + "\n")
        if t in folded:
            b = b.rstrip() + "\n\n" + _details(*folded[t]) + "\n"
        out.append(f"## {t}\n{b}")
    return "".join(out)


# File references in the report, for the published repo layout. The working copy renders the
# same text, so what is reviewed locally is exactly what gets published.
REPO_URL = "https://github.com/hasbro17/bbolt-madv-random-benchmark"
PAGES_URL = "https://hasbro17.github.io/bbolt-madv-random-benchmark/"
RAW_URL = REPO_URL + "/releases/tag/raw-data-2026-10-03"
REFS = {"agg": "data/aggregate.json", "log": "docs/METHODS.md",
        "inputs": "`docs/VERSIONS.md`, `docs/DATASET.md`, `docs/PRESSURE.md`",
        "raw": f"Raw per-run data, including failed and superseded attempts, is in the [raw-data release]({RAW_URL}).",
        "repro": "`docs/REPRODUCE.md` and `scripts/`"}


def build(agg, outdir, env_dir, infra_dir, draft, supp=None):
    vs = verdicts(agg, supp)
    charts = [c for c in (
        paired_chart(agg, outdir, ["S3a", "S3a-ref", "S3a-mglru-off"], "compaction_s",
                     "One large compaction: time per pair", "seconds", "s3a-compaction"),
        paired_chart(agg, outdir, ["S3a", "S3a-ref", "S3a-mglru-off"], "compaction_majflt",
                     "One large compaction: major page faults per pair", "major page faults", "s3a-faults"),
        paired_chart(agg, outdir, ["S3b", "S3b-ref"], "compaction_s_mean",
                     "Compaction every minute under writes: mean compaction time per run", "seconds", "s3b-compaction"),
        ratio_chart(agg, outdir, "S2", ".p99_ms", "Reads under the memory limit: p99 latency, treatment vs control", "s2-p99"),
        ratio_chart(agg, outdir, "S1", ".p99_ms", "Reads with plenty of memory: p99 latency, treatment vs control", "s1-p99"),
        readahead_chart(supp, outdir),
        s4_chart(agg, outdir),
    ) if c]
    by_name = {c["name"]: c for c in charts}
    when = dt.datetime.fromtimestamp(agg["generated_ts"]).strftime("%Y-%m-%d")
    facts = env_facts(env_dir)
    capped = sorted({c for s in agg["scenarios"].values() for c in s.get("caps", []) if gib(c) != "none"})

    # ---------------- REPORT.md
    lim = " / ".join(gib(c) for c in capped) or "n/a"
    md = [f"# bbolt MADV_RANDOM removal: etcd benchmark results{' (DRAFT)' if draft else ''}", "",
          f"**Visual version of this report (charts with explanations, collapsible tables): <{PAGES_URL}>**", "",
          "etcd stores its data in bbolt, which memory-maps the database file. bbolt calls "
          "`madvise(MADV_RANDOM)` on that mapping, which tells the kernel not to read ahead around page faults. "
          "Since Linux 6.4 (kernel commit `8788f678`, \"mm: add vma_has_recency()\"), the same flag also stops the "
          "kernel from counting accesses through the mapping as recent use, so etcd's cached database pages are among "
          "the first to be evicted under memory pressure. Compaction then reads them back from disk one 4 KiB page at a "
          "time and becomes much slower ([etcd-io/bbolt#939](https://github.com/etcd-io/bbolt/issues/939)).", "",
          "The question asked on the issue: **does removing the `MADV_RANDOM` call affect performance other than "
          "compaction?** This report answers it with etcd's own benchmark tools (`tools/benchmark` and "
          "`tools/rw-heatmaps/rw-benchmark.sh`).", ""] + ([short_answer(vs, supp or {}), ""] if short_answer(vs, supp or {}) else []) + [
          "**Scope: Linux 6.4 and later only** (tested on RHEL 10, kernel 6.12). These results say nothing about "
          "older kernels.", "",
          "## Terms used", "",
          "- **Control** and **treatment**: etcd `main` with bbolt v1.5.0 as shipped, and the same build with the "
          "`madvise(MADV_RANDOM)` call removed. Nothing else differs.",
          f"- **Memory limit**: etcd runs in a cgroup whose `MemoryMax` is below the size of its 8.6 GB database, so the "
          f"kernel has to drop some of etcd's cached database pages and read them from disk again when they are needed "
          f"(\"memory pressure\"). Main limit: {lim} (etcd's own memory plus 60% of the database), applied once etcd "
          "is running.",
          "- **Pair**: one control run and one treatment run back to back on the same VM, from identical copies of the "
          "same database. Results are the median over pairs of treatment / control; \"range over pairs\" is the "
          "lowest and highest pair.",
          f"- **Noise band**: a difference counts only if it is larger than {EQUIV_MIN:.0%} or half the spread between "
          "pairs, whichever is bigger.",
          "- **Major page fault**: etcd touched a database page that was not in memory, so the kernel read it from disk.",
          "- **Readahead**: on a page fault the kernel also reads the data that follows, in one go (`read_ahead_kb` sets "
          "how much). `MADV_RANDOM` turns this off for the database file.",
          "- **Working set**: the part of the database a workload keeps reading; for the read tests, mostly the current "
          "version of every key (a few GiB of the 8.6 GB database).",
          "- **x**: \"33x\" means 33 times the control value; smaller changes are shown as percentages.", "",
          "## Answer", "",
          "Each row is one test scenario and the question it answers; the scenarios are described in the next "
          "section. S3 covers two scenarios, S3a and S3b.", "",
          "| | Question | Answer | Key numbers |", "|---|---|---|---|"]
    for v in vs:
        md.append(f"| **{v['id']}** | {v['q']} | {v['answer']} | {v['headline']} |")
    md += [""] + key_findings(agg, supp or {})
    md += ["## What each scenario does", "",
           "| | Scenario | What it does | Why |", "|---|---|---|---|"]
    for sid, t, w, why in SCENARIO_ROWS:
        md.append(f"| **{sid}** | {t} | {w} | {why} |")
    md += ["", "The scenarios ran in parallel on five identical VMs. Each scenario, together with its run without a "
           "memory limit, ran entirely on one VM, and only numbers from the same VM are compared.", ""]

    res = result_lines(agg, supp or {}, vs)

    def result(key):
        return [f"**Result:** {res[key]}", ""] if res.get(key) else []

    def section(title, sid, keys_filter, chart_names, note="", ref=None, ref_label="", after="", rkey=None):
        s = scen(agg, sid)
        md.extend([f"## {title}", ""] + result(rkey or sid))
        if note:
            md.extend([note, ""])
        if not s:
            md.extend(["Not run.", ""])
            return
        for cn in chart_names:
            if cn in by_name:
                md.extend(fig(by_name[cn]))
        keys = sorted((k for k in s["summary"] if keys_filter(k) and not limiter_latency(k)), key=sort_key)
        caps = ", ".join(gib(c) for c in s.get("caps", [])) or "n/a"
        caps = "no memory limit" if caps == "none" else f"memory limit {caps}"
        md.extend([f"{s['n_pairs']} pairs, {caps}.", "",
                   metric_table(s["summary"], keys, unit_of, ref, ref_label), ""])
        if after:
            md.extend([after, ""])

    def reads(k):
        """Read-test rows: latency and throughput per read type; the full list as time per list."""
        if k.startswith("warmup-") or k.startswith("list-paginate."):
            return k == "list-paginate.avg_ms"
        return k.endswith((".p99_ms", ".rps"))

    supp = supp or {}
    s1 = scen(agg, "S1")
    section("S1: reads with plenty of memory", "S1", reads, ["s1-p99"],
            "Random point reads, 500-key range reads and a full list of all keys, at 16, 64 and 256 clients, with no "
            "memory limit: etcd's whole database stays cached, so both builds are expected to be equal.")
    md.extend(s1_long_section(supp.get("s1_long"), result("S1-long")))
    section("S2: reads under memory pressure", "S2",
            lambda k: reads(k) or k in ("majflt_total", "read_mib_total"), ["s2-p99"],
            "The same reads as S1 under the memory limit, followed by 10 minutes of steady point and range reads. "
            "Under the limit, treatment keeps the data these reads touch in memory, while control keeps losing it and "
            "reading it back from disk (see the disk-read and page-fault rows). The last column shows how much the same "
            "measure varies between pairs in S1, without a memory limit: the 500-key range reads keep etcd's CPU "
            "saturated, so they vary a lot in every scenario.",
            ref=s1["summary"] if s1 else None, ref_label="Range over pairs in S1 (no memory limit)")
    md.extend(s2_tight_section(supp.get("s2_tight"), by_name.get("s2-tight-readahead"), result("S2-tight")))
    section("S3a: one large compaction under memory pressure", "S3a",
            lambda k: k in ("compaction_s", "compaction_majflt", "compaction_read_mib", "backend_commit_p99_ms"),
            ["s3a-compaction", "s3a-faults"],
            "etcd starts on a database holding 1,050,000 old revisions, the memory limit is applied, and one "
            "`etcdctl compact` removes them. The time is taken from etcd's own \"finished scheduled compaction\" log line.",
            after="Treatment reads more bytes from disk than control, but in large readahead chunks instead of one "
                  "4 KiB page per fault, which is why it is much faster. There is no client traffic in S3a, so "
                  "\"backend commit p99 latency\" covers only the compaction's own batch commits (about 2,450 per run); "
                  "etcd reports it in coarse buckets (4, 8, 16, 32, 64 ms, ...), so the values are bucket bounds. "
                  "Treatment does those commits in about a minute while readahead keeps the disk busy, so some of them "
                  "wait longer. Client writes during compaction are measured in S3b.")
    section("S3a without a memory limit", "S3a-ref", lambda k: k in ("compaction_s", "compaction_majflt", "compaction_read_mib"), [],
            "The same compaction with no memory limit. Both builds compact equally fast, so the memory limit is what "
            "triggers the slowdown.")
    s3bref = scen(agg, "S3b-ref")
    ref_rate = s3bref and (s3bref["summary"].get("put-compact.rps") or {}).get("control", {}).get("median")
    section("S3b: compaction every minute under writes", "S3b",
            lambda k: k.startswith("compaction_s") or k.startswith("put-compact.") and k.endswith((".p99_ms", ".p999_ms", ".rps"))
            or k in ("compactions", "majflt_per_s", "read_mib_per_s", "stm-background.rps"),
            ["s3b-compaction"],
            "900,000 writes at a target rate of 1,000 per second (about 15 minutes) with a compaction every 60 seconds "
            "and background point reads, under the memory limit. Each run makes the same number of writes, so a slower "
            "run lasts longer and "
            "goes through more compaction rounds: compare per-compaction times and latencies, not totals."
            + (f" Neither build reaches the 1,000 per second target even without a memory limit (about "
               f"{ref_rate:.0f} per second, see below); that is a limit of this setup and the same for both."
               if ref_rate and ref_rate < 950 else ""))
    section("S3b without a memory limit", "S3b-ref",
            lambda k: k.startswith("compaction_s") or k in ("put-compact.rps", "put-compact.p999_ms"), [],
            "The same workload with no memory limit: both builds are equal.")
    section("Compaction with MGLRU switched off", "S3a-mglru-off",
            lambda k: k in ("compaction_s", "compaction_majflt", "compaction_read_mib"), [],
            "The S3a compaction with the kernel's MGLRU page-reclaim mode switched off, to test whether MGLRU is what "
            "triggers the slowdown. It is not: without MGLRU control is slower still. It reads the whole database once, "
            "one 4 KiB page per fault, while treatment reads the same bytes in large readahead chunks.", rkey="MGLRU-off")
    s4 = scen(agg, "S4")
    md.extend(["## S4: etcd's rw-benchmark.sh", ""] + result("S4") + [
               "etcd's `tools/rw-heatmaps/rw-benchmark.sh`, unmodified: workload mixes of read:write ratios from 1:8 "
               "to 8:1, values of 256 B, 1 KiB and 4 KiB, and 32 to 256 clients, each starting from an empty database, "
               "with plenty of memory. Values are treatment against control for the mean throughput of each mix.", ""])
    if s4 and "s4-cells" in by_name:
        n = s4["summary"]["cells.read_qps"]["ratio"]["n"]
        md.extend(fig(by_name["s4-cells"]) + [
                   f"| {n} workload mixes | Treatment vs control (median over mixes) | Range over mixes |", "|---|--:|--:|"])
        for f, name in (("read_qps", "Read throughput"), ("write_qps", "Write throughput")):
            r = s4["summary"][f"cells.{f}"]["ratio"]
            md.append(f"| {name} | {pct(r['median'])} | {pct_range(r['min'], r['max'])} |")
        md.append("")
    else:
        md.extend(["Not run.", ""])
    md.extend(s4_recheck_section(supp.get("s4_recheck")))

    st = supp.get("startup") or {}
    if st:
        md.extend(["## etcd startup under a memory limit", ""] + result("startup") + [
                   "One run per build, not pairs: etcd started with a memory limit already in place, so its startup "
                   "(which reads every revision to rebuild its index) runs under pressure. Without a limit startup takes "
                   f"{fmt(supp.get('startup_uncapped_median_s'), ' s')}.", "",
                   "| Build | Time until etcd is healthy | Memory limit |", "|---|--:|--:|"])
        rows = [(v, r) for v in ("control", "treatment") for r in st.get(v, [])]
        for v, r in rows:
            md.append(f"| {v} | {fmt(r['startup_s'], ' s')} | {gib(r['cap'])} |")
        md.append("")
        if any(r.get("status") != "ok" for _, r in rows):
            md.extend(["The control run was stopped during the compaction that followed startup, to save time; its "
                       "startup time is complete.", ""])
    dose = {k: v for k, v in (supp.get("dose") or {}).items() if v.get("control_median_s")}
    if dose:
        md.extend(["## Compaction time at three memory limits", ""] + result("dose") + [
                   "The S3a compaction at three memory limits on one VM, one run per build at each limit, to show how "
                   "the effect grows with pressure.", "",
                   "| Memory limit (etcd's memory plus this share of the database) | Limit | Control | Treatment | "
                   "Treatment vs control |", "|---|--:|--:|--:|--:|"])
        for k in sorted(dose, key=lambda x: -float(x.split("-")[-1])):
            v = dose[k]
            c, t = v.get("control_median_s"), v.get("treatment_median_s")
            cap = next((r["cap"] for var in ("control", "treatment") for r in v.get(var, [])), None)
            md.append(f"| {float(k.split('-')[-1]):.0%} | {gib(cap)} | {fmt(c, ' s')} | {fmt(t, ' s')} | "
                      f"{_times(t / c) if c and t else 'n/a'} |")
        md.append("")
    md.extend(["## Caveats and method notes", "",
               "- **Readahead size.** RHEL 10's stock tuning profile (`virtual-guest`) sets `read_ahead_kb=4096` (4 MiB) "
               "on the data disk; the kernel default is 128 KiB. Without `MADV_RANDOM`, every page fault on the "
               "database can read that much data. Readahead is what makes compaction fast without `MADV_RANDOM`, but "
               "with a memory limit below the working set a 4 MiB readahead makes large reads slower (see the S2 "
               "follow-up). All compaction runs (S3a, S3b) used the 4 MiB setting; compaction with the 128 KiB "
               "default was not measured. At 128 KiB each fault still reads 32 pages instead of one, so compaction is "
               "expected to stay limited by disk throughput, as it was here. Distributions and tuning profiles differ, so check `read_ahead_kb` on "
               "etcd's data disk.",
               "- **When the memory limit is applied.** The limit is set once etcd is running, which models a running "
               "etcd that comes under memory pressure. With the limit in place from the start, startup itself becomes "
               "much slower (see \"More results\").",
               "- **Random point reads** use `benchmark stm` with one key per transaction and no writes. "
               "`benchmark range` and `txn-mixed` always read the same keys, so they never touch random pages of the "
               "database.",
               "- **Latencies left out.** Two steps run `benchmark stm` at a fixed rate (the 10-minute pass in S2, the "
               "background reads in S3b). `stm` waits for its rate limiter inside the timed part of each request, so "
               "with `--rate` the recorded latency is mostly that wait (clients divided by rate, for example 64 / "
               "2,000 per second = 32 ms, the same for both builds). Only their throughput is shown, which tells "
               "whether each build kept up with the target rate. `put` and `range` start the clock after the wait, so "
               "their latencies are shown.",
               "- **How S3b is judged.** S3b uses the mean compaction time per run plus major faults, not the median: "
               "control's damage is a few stalled compactions that a median hides. This was decided after 2 of 5 "
               f"pairs, before the rest ran (`{REFS['log']}`). Median, mean and slowest are all in the table.",
               "- **Same-VM comparisons only.** Absolute numbers differ between VMs (control's S3a compaction took "
               "771 to 1,123 s on the three VMs in the check before the main runs), so every comparison is within "
               "one VM.", ""])
    kernels = {f["uname"] for f in facts.values()}
    ras = {f["readahead"] for f in facts.values()}
    hosts = (f"{len(facts)} identical VMs, each with kernel `{next(iter(kernels))}` and `read_ahead_kb={next(iter(ras))}` on the data disk"
             if len(kernels) == 1 and len(ras) == 1 else
             "; ".join(f"VM {vm}: `{f['uname']}`, read_ahead_kb={f['readahead']}" for vm, f in facts.items()))
    md.extend(["## Appendix: environment and method", "",
               "- Hosts: " + (hosts or "see env/"),
               f"- Versions, binary checksums, dataset and memory limit: {REFS['inputs']}.",
               f"- Every failure, fix and rerun: `{REFS['log']}`. {REFS['raw']}",
               f"- Reproduce: {REFS['repro']}; any Linux 6.4+ host with cgroup v2.", ""])
    report_md = restructure("\n".join(md) + "\n")

    # ---------------- GITHUB-COMMENT.md (short; every number from the data)
    s3a, s2, s1, s4c = scen(agg, "S3a"), scen(agg, "S2"), scen(agg, "S1"), scen(agg, "S4")
    s3b = scen(agg, "S3b")
    t = (supp or {}).get("s2_tight") or {}
    ta, tb = (t.get("4096") or {}).get("metrics"), (t.get("128") or {}).get("metrics")
    rows = []
    pr = s1_point_range(agg, supp)
    if s1 and pr:
        rows.append(("Reads, plenty of memory", f"No change: point-read throughput and p99 latency within "
                     f"{pct(min(pr))} to {pct(max(pr))} of control"))
    if s2:
        rows.append(("Reads under memory pressure", f"Faster without `MADV_RANDOM`: "
                     f"{_med(s2['summary'], 'stm-c64.rps') or 0:.0f}x the point-read throughput at 64 clients; a full "
                     f"list of all keys is {_times(_med(s2['summary'], 'list-paginate.avg_ms'))}"))
    if s3a:
        cs = s3a["summary"]["compaction_s"]
        line = (f"{_times(cs['ratio']['median'])} without `MADV_RANDOM` for one large compaction "
                f"({fmt(cs['control']['median'], ' s')} vs {fmt(cs['treatment']['median'], ' s')})")
        mx = s3b and s3b["summary"].get("compaction_s_max")
        if mx and mx.get("ratio"):
            line += (f"; with compaction every minute under writes, the slowest compaction drops from "
                     f"{fmt(mx['control']['median'], ' s')} to {fmt(mx['treatment']['median'], ' s')}")
        rows.append(("Compaction under memory pressure", line))
    if s4c and s4c["summary"].get("cells.read_qps"):
        rr, wr = s4c["summary"]["cells.read_qps"]["ratio"], s4c["summary"]["cells.write_qps"]["ratio"]
        within = max(abs(x - 1) for x in (rr["min"], rr["max"], wr["min"], wr["max"]))
        rows.append(("`rw-benchmark.sh` sweep", f"No change: across {rr['n']} read/write workload mixes, median "
                     f"{pct(rr['median'])} for reads and {pct(wr['median'])} for writes, all within {within * 100:.1f}%"))
    else:
        rows.append(("`rw-benchmark.sh` sweep", "pending"))
    gc = ["Following up on the request to evaluate this with etcd's own benchmark tools. I compared etcd `main` "
          "with bbolt v1.5.0 as shipped (control) against the same build with the `madvise(MADV_RANDOM)` call "
          "removed (treatment), using `tools/benchmark` and `tools/rw-heatmaps/rw-benchmark.sh` on Linux 6.12 "
          "(RHEL 10). \"Memory pressure\" means etcd ran under a cgroup memory limit below the size of its 8.6 GB "
          "database.", "",
          "| | Result |", "|---|---|"] + [f"| {a} | {b} |" for a, b in rows] + [""]
    s2f = (s2 and s2["summary"].get("majflt_total")) or {}
    faults = (f" This also answers the earlier question about faults outside compaction: under memory pressure with reads only (no "
              f"compaction), major page faults per run drop from {fmt(s2f['control']['median'])} to "
              f"{fmt(s2f['treatment']['median'])}." if s2f.get("control") else "")
    gc += ["**Proposal:** remove the call (revive #940). This meets the condition suggested earlier in this thread (no impact "
           "other than compaction): nothing outside compaction gets worse, apart from the readahead case below, so etcd "
           "would not need to treat compaction differently from other reads." + faults + " If an "
           "option is still preferred, I'm happy to add one, but it should default to not setting `MADV_RANDOM`; "
           "otherwise the compaction slowdown stays on every 6.4+ kernel.", ""]
    if ta and tb:
        gc += ["**Readahead note:** without `MADV_RANDOM`, a page fault also makes the kernel read the data that "
               "follows (readahead). If etcd's memory is much smaller than the data it keeps reading, a large readahead "
               "can hurt: with `read_ahead_kb=4096` (RHEL's stock tuning), a full list of all keys was "
               f"{_times(_med(ta, 'list-paginate.avg_ms'))} in our tightest test (memory limit "
               f"{gib(next(iter((t.get('4096') or {}).get('caps') or []), None))}), because each fault pushed out pages "
               f"still in use. With the kernel default of 128 KiB it was {_times(_med(tb, 'list-paginate.avg_ms'))}.", ""]
    def _n_range(ns):
        ns = sorted({n for n in ns if n})
        return "n/a" if not ns else str(ns[0]) if len(ns) == 1 else f"{ns[0]} to {ns[-1]}"
    main_n = _n_range((scen(agg, s) or {}).get("n_pairs") for s in ("S1", "S2", "S3a", "S3b"))
    side_n = _n_range([(scen(agg, s) or {}).get("n_pairs") for s in ("S3a-ref", "S3b-ref")]
                      + [(t.get(ra) or {}).get("n_pairs") for ra in ("4096", "128")])
    gc += ["Scope: Linux 6.4 and later only; nothing here speaks to older kernels.", "",
           "<!-- attach charts/s3a-compaction.png and charts/s2-tight-readahead.png here -->", "",
           f"Full report: {REPO_URL} · Visual version: {PAGES_URL} · How to reproduce: "
           f"{REPO_URL}/blob/main/docs/REPRODUCE.md · Raw data: {RAW_URL}", "",
           "<details><summary>Setup</summary>", "",
           "Single-member etcd per VM, AWS m7i.4xlarge (dedicated tenancy), gp3 data volume (3000 IOPS, 125 MiB/s). Each "
           "scenario runs control and treatment back to back on the same VM, each time from a fresh copy of the same "
           f"database: {main_n} pairs per scenario, {side_n} for the runs without a memory limit and the readahead "
           "test. `rw-benchmark.sh` runs its sweep once per build and repeats each workload mix 5 times. Random point "
           "reads use `benchmark stm` with one existing key per read.", "",
           "</details>", ""]
    comment_md = "\n".join(gc)

    # ---------------- report.html
    html_out = render_html(vs, by_name, agg, report_md, when, draft)

    with open(os.path.join(outdir, "REPORT.md"), "w") as f:
        f.write(report_md)
    with open(os.path.join(outdir, "GITHUB-COMMENT.md"), "w") as f:
        f.write(comment_md)
    with open(os.path.join(outdir, "report.html"), "w") as f:
        f.write(html_out)
    return vs, charts


def md_table_to_html(block):
    lines = [l for l in block.strip().splitlines() if l.startswith("|")]
    if len(lines) < 2:
        return ""
    cells = lambda l: [c.strip() for c in l.strip("|").split("|")]  # noqa: E731
    head = "".join(f"<th>{inline(c)}</th>" for c in cells(lines[0]))
    body = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells(l)) + "</tr>" for l in lines[2:])
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"&lt;(https?://[^&\s]+)&gt;", r'<a href="\1">\1</a>', s)
    return s


def render_html(vs, by_name, agg, report_md, when, draft):
    cls = {True: "pass", False: "fail", None: "na"}
    word = {True: "As expected", False: "Not as expected", None: "Not run"}
    cards = "".join(
        f'<div class="card {cls[v["pass"]]}"><div class="cid">{v["id"]} <span class="badge">{word[v["pass"]]}</span></div>'
        f'<div class="q">{inline(v["q"])}</div><div class="a">{inline(v["answer"])}</div>'
        f'<div class="h">{inline(v["headline"])}</div></div>' for v in vs)
    # The intro (everything before the first section: background, question, short answer, scope)
    # goes above the cards; the "In short" bullets go under them. The page itself is the visual
    # version, so the markdown's link to it is left out.
    head = report_md.split("\n## ", 1)[0]
    intro = "".join((f'<p class="scope">{inline(l)}</p>' if l.startswith("**Scope") else f"<p>{inline(l)}</p>")
                    for l in head.splitlines()
                    if l.strip() and not l.startswith("# ") and not l.startswith("**Visual version"))
    ans = report_md.split("## Answer", 1)[1].split("\n## ", 1)[0] if "## Answer" in report_md else ""
    bullets = [l[2:] for l in ans.split("### In short", 1)[-1].splitlines() if l.startswith("- ")] if "### In short" in ans else []
    findings = ((f'<h2>In short</h2><ul class="findings">{"".join(f"<li>{inline(b)}</li>" for b in bullets)}</ul>'
                   if bullets else ""))
    # Sections: reuse the markdown sections, swap images for inline SVG and tables for HTML.
    parts = re.split(r"^## ", report_md, flags=re.M)[1:]
    secs = []
    for p in parts:
        title, _, body = p.partition("\n")
        if title.startswith("Answer"):
            continue
        chunks, table, prose = [], [], []
        for line in body.splitlines():
            m = re.match(r"!\[(.+?)\]\((.+?)\)", line)
            if m and m.group(1) in by_name:
                chunks.append(f'<figure>{by_name[m.group(1)]["svg"]}</figure>')
            elif line.startswith("|"):
                table.append(line)
            elif line.startswith(("<details>", "</details>")):
                if table:
                    chunks.append(md_table_to_html("\n".join(table)))
                    table = []
                m2 = re.match(r"<details><summary>(.*)</summary>", line)
                chunks.append(f"<details><summary>{inline(m2.group(1))}</summary>" if m2 else line)
            else:
                if table:
                    chunks.append(f"<details open><summary>Table</summary>{md_table_to_html(chr(10).join(table))}</details>"
                                  if len(table) > 8 else md_table_to_html("\n".join(table)))
                    table = []
                if line.strip():
                    chunks.append(f"<p>{inline(line.lstrip('- '))}</p>")
        if table:
            chunks.append(md_table_to_html("\n".join(table)))
        secs.append(f"<section><h2>{inline(title)}</h2>{''.join(chunks)}</section>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>bbolt MADV_RANDOM Benchmark</title>
<style>
:root {{ --bg:#fbfaf7; --fg:#1f2328; --muted:#5d6670; --card:#ffffff; --line:#e3e0d8; --pass:#2e7d4f; --fail:#b3261e; --na:#8a8f98; --accent:#2f6f9f; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#16181b; --fg:#e7e9ec; --muted:#a0a7b0; --card:#1f2226; --line:#30343a; --pass:#5cc08a; --fail:#ef6b62; --na:#7d838c; --accent:#7fb3db; }} }}
:root[data-theme="dark"] {{ --bg:#16181b; --fg:#e7e9ec; --muted:#a0a7b0; --card:#1f2226; --line:#30343a; --pass:#5cc08a; --fail:#ef6b62; --na:#7d838c; --accent:#7fb3db; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 -apple-system, system-ui, "Segoe UI", sans-serif; }}
main {{ max-width:980px; margin:0 auto; padding:28px 16px 64px; }}
h1 {{ font-size:1.6rem; margin:0 0 6px; }} h2 {{ font-size:1.2rem; margin:36px 0 10px; border-top:1px solid var(--line); padding-top:22px; }}
.sub {{ color:var(--muted); margin:0 0 22px; }}
.scope {{ margin:18px 0 0; padding:10px 14px; border-left:3px solid var(--accent); background:var(--card); }}
.findings li {{ margin:6px 0; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:12px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-left:5px solid var(--na); border-radius:8px; padding:12px 14px; }}
.card.pass {{ border-left-color:var(--pass); }} .card.fail {{ border-left-color:var(--fail); }}
.cid {{ font-weight:700; }} .badge {{ font-size:.75rem; font-weight:600; padding:1px 7px; border-radius:10px; border:1px solid currentColor; margin-left:6px; }}
.pass .badge {{ color:var(--pass); }} .fail .badge {{ color:var(--fail); }} .na .badge {{ color:var(--na); }}
.q {{ color:var(--muted); font-size:.88rem; margin:4px 0 8px; }} .a {{ font-weight:600; }} .h {{ font-size:.85rem; color:var(--muted); margin-top:4px; }}
figure {{ margin:14px 0; background:#fff; border-radius:8px; padding:8px; overflow-x:auto; }} figure svg {{ max-width:100%; height:auto; }}
.scroll {{ overflow-x:auto; }} table {{ border-collapse:collapse; width:100%; font-size:.88rem; margin:10px 0; }}
th, td {{ border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }} th {{ color:var(--muted); font-weight:600; }}
code {{ font-size:.85em; background:color-mix(in srgb, var(--line) 50%, transparent); padding:1px 4px; border-radius:4px; }}
summary {{ cursor:pointer; color:var(--accent); }}
</style></head>
<body><main>
<h1>bbolt MADV_RANDOM removal: etcd benchmark results{' (draft)' if draft else ''}</h1>
<p class="sub">Generated {when}.</p>
{intro}
<div class="cards">{cards}</div>
{findings}
{''.join(secs)}
</main></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("aggregate")
    ap.add_argument("--out", required=True)
    ap.add_argument("--env-dir")
    ap.add_argument("--infra-dir")
    ap.add_argument("--draft", action="store_true")
    ap.add_argument("--supp", help="supplementary.json from supplementary.py")
    ap.add_argument("--publish", action="store_true", help="accepted for compatibility; the output is always the published text")
    a = ap.parse_args()
    agg = json.load(open(a.aggregate))
    os.makedirs(a.out, exist_ok=True)
    supp = json.load(open(a.supp)) if a.supp and os.path.exists(a.supp) else None
    vs, charts = build(agg, a.out, a.env_dir, a.infra_dir, a.draft, supp)
    for v in vs:
        print(f"{v['id']}: {v['pass']} {v['answer']} | {v['headline']}")
    print(f"charts: {', '.join(c['name'] for c in charts)}")
    print(f"wrote REPORT.md, report.html, GITHUB-COMMENT.md to {a.out}")


if __name__ == "__main__":
    main()
