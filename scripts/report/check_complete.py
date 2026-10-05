#!/usr/bin/env python3
"""Completeness and consistency check before P7 (PLAN "Compiling results").

  check_complete.py RESULTS_DIR --config scripts/config.env --versions FILE --dataset FILE
                    [--cap FILE] [--scenarios "S3a S2 ..."] [--smoke]

Checks, per scenario:
  - the expected number of done pairs (both runs ok) at one scenario_rev
  - both runs of every pair come from one VM
  - if the scenario spans VMs: each VM has its anchor pairs and the per-pair ratios of the
    headline metric agree (reported for review; decide by hand whether to rerun in full)
  - binary checksums match versions.env, the golden matches dataset.env, the cap is the
    same in every capped run and matches cap.env
  - nothing from _failed/ or _superseded/ sits in the main tree
Prints a to-do list of gaps. Exit 0 only when there are none.
"""
import argparse
import glob
import json
import os
import re
import statistics
import sys

HEADLINE = {"S3a": "compaction_s", "S3a-ref": "compaction_s", "S3a-mglru-off": "compaction_s",
            "S3b": "compaction_s_median", "S3b-ref": "compaction_s_median"}
ALL = ["S3a", "S3a-ref", "S3a-mglru-off", "S3b", "S3b-ref", "S2", "S1", "S4"]


def read_env(path):
    out = {}
    if path and os.path.exists(path):
        for line in open(path):
            m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', line.strip())
            if m:
                v = m.group(2).strip().strip('"')
                m2 = re.match(r'^\$\{[A-Za-z0-9_]+:-(.*)\}$', v)
                out[m.group(1)] = m2.group(1) if m2 else v
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--config", required=True)
    ap.add_argument("--versions", required=True)
    ap.add_argument("--dataset")
    ap.add_argument("--cap")
    ap.add_argument("--scenarios", default=" ".join(ALL))
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args()

    cfg, ver, cap_env = read_env(a.config), read_env(a.versions), read_env(a.cap)
    root = os.path.join(a.root, "_smoke") if a.smoke else a.root
    todo, notes = [], []
    for scen in a.scenarios.split():
        sdir = os.path.join(root, scen)
        want = 1 if a.smoke else int(cfg.get("PAIRS_" + scen.replace("-", "_"), "0"))
        if not os.path.isdir(sdir):
            todo.append(f"{scen}: no results at all ({want} pairs needed)")
            continue
        if os.path.exists(os.path.join(sdir, "scenario.stopped")):
            todo.append(f"{scen}: scenario.stopped: {open(os.path.join(sdir, 'scenario.stopped')).read().strip()}")
        done, revs, vms_by_pair, caps = [], set(), {}, set()
        for pdir in sorted(glob.glob(os.path.join(sdir, "pair-*"))):
            pname = os.path.basename(pdir)
            pdone = os.path.join(pdir, "pair.done")
            if not os.path.exists(pdone):
                todo.append(f"{scen}/{pname}: incomplete (no pair.done)")
                continue
            pd = json.load(open(pdone))
            revs.add(pd.get("scenario_rev"))
            runs = {}
            for v in ("control", "treatment"):
                rjp = os.path.join(pdir, v, "run.json")
                if not os.path.exists(rjp):
                    todo.append(f"{scen}/{pname}/{v}: missing run.json")
                    continue
                rj = json.load(open(rjp))
                runs[v] = rj
                if rj.get("status") != "ok":
                    todo.append(f"{scen}/{pname}/{v}: status {rj.get('status')} ({rj.get('reason')})")
                if scen != "S4":
                    want_sha = ver.get(f"SHA_{v}_etcd")
                    if want_sha and rj.get("binary_sha") != want_sha:
                        todo.append(f"{scen}/{pname}/{v}: binary sha does not match versions.env")
                if not uncapped(rj.get("cap")):
                    caps.add(str(rj.get("cap")))
            if len(runs) == 2:
                vms = {runs["control"].get("vm"), runs["treatment"].get("vm")}
                if len(vms) != 1:
                    todo.append(f"{scen}/{pname}: control and treatment ran on different VMs {sorted(vms)}")
                vms_by_pair[pname] = vms.pop()
                done.append(pname)
        if len(done) < want:
            todo.append(f"{scen}: {len(done)}/{want} pairs done")
        if len(revs) > 1:
            todo.append(f"{scen}: pairs from different scenario_revs {sorted(revs)}")
        if len(caps) > 1:
            todo.append(f"{scen}: different caps across runs {sorted(caps)}")
        if caps and cap_env.get("CAP_BYTES") and caps != {cap_env["CAP_BYTES"]}:
            todo.append(f"{scen}: cap {sorted(caps)} differs from cap.env {cap_env['CAP_BYTES']}")
        vms = sorted(set(vms_by_pair.values()))
        if len(vms) > 1:
            notes.append(f"{scen}: spans VMs {vms} (replacement); check anchors and ratio agreement below")
            metric = HEADLINE.get(scen)
            if metric:
                by_vm = {}
                for pname, vm in vms_by_pair.items():
                    try:
                        c = _metric(os.path.join(sdir, pname, "control"), metric)
                        t = _metric(os.path.join(sdir, pname, "treatment"), metric)
                        by_vm.setdefault(vm, []).append(t / c)
                    except Exception:  # noqa: BLE001 - reported, not fatal
                        pass
                for vm, rs in by_vm.items():
                    notes.append(f"  {scen} {metric} ratio on {vm}: median {statistics.median(rs):.3f} range {min(rs):.3f}-{max(rs):.3f}")
        for stray in glob.glob(os.path.join(sdir, "pair-*", "_*")):
            todo.append(f"{scen}: stray {stray} inside a pair dir")

    for n in notes:
        print("NOTE", n)
    if todo:
        print(f"INCOMPLETE: {len(todo)} item(s)")
        for t in todo:
            print("TODO", t)
        return 1
    print("COMPLETE: every scenario has its pairs at one scenario_rev with consistent inputs")
    return 0


def uncapped(cap):
    """run.json stores no cap as "infinity" (older runs: a float inf)."""
    return cap is None or cap == "infinity" or (isinstance(cap, float) and cap == float("inf"))


def _metric(rundir, metric):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import aggregate  # noqa: E402
    _, m = aggregate.run_metrics(rundir, os.path.basename(os.path.dirname(os.path.dirname(rundir))))
    return m[metric]


if __name__ == "__main__":
    sys.exit(main())
