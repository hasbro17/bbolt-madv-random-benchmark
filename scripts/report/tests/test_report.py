#!/usr/bin/env python3
"""P0 step 10: tests for aggregate.py, check_complete.py, validate_s4.py and render.py.

Synthetic results trees cover the edge cases; the real outputs captured on the Mac
(testdata/real/) check that every parser handles the actual formats.

  python3 -m unittest discover -s scripts/report/tests     (render tests need matplotlib)
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.dirname(HERE)
SCRIPTS = os.path.dirname(REPORT)
sys.path.insert(0, REPORT)
import aggregate  # noqa: E402

BENCH = """2026/10/02 16:16:40 INFO: [core] noise line
Summary:
  Total:\t{total:.4f} secs.
  Slowest:\t0.1406 secs.
  Fastest:\t0.0015 secs.
  Average:\t0.0109 secs.
  Stddev:\t0.0087 secs.
  Requests/sec:\t{rps:.4f}

Latency distribution:
  10% in 0.0047 secs.
  50% in {p50:.4f} secs.
  90% in 0.0195 secs.
  99% in {p99:.4f} secs.
  99.9% in 0.1010 secs.
"""
LOG_LINE = ('{{"level":"info","ts":"2026-10-02T16:08:21.548-0700","caller":"mvcc/kvstore_compaction.go:77",'
            '"msg":"finished scheduled compaction","compact-revision":30001,"took":"{took}",'
            '"number-of-keys-compacted":20000}}\n')


def write_run(d, status="ok", vm="A", sha="sha-x", took=None, bench=None, samples=None, cap=1000):
    os.makedirs(d, exist_ok=True)
    json.dump({"status": status, "vm": vm, "binary_sha": sha, "cap": cap, "golden": "history",
               "start_ts": 1000, "end_ts": 1100, "scenario_rev": "r1"}, open(os.path.join(d, "run.json"), "w"))
    with open(os.path.join(d, "marks.csv"), "w") as f:
        f.write("1000.0,client-start\n1010.0,compact-start\n1090.0,compact-end\n1100.0,client-end\n")
    if took:
        open(os.path.join(d, "etcd.log"), "w").write(LOG_LINE.format(took=took))
    for name, vals in (bench or {}).items():
        open(os.path.join(d, f"bench-{name}.txt"), "w").write(BENCH.format(**vals))
    if samples:
        with open(os.path.join(d, "samples.csv"), "w") as f:
            f.write("ts,majflt,disk_sectors_read,cg_memory_current\n")
            for ts, maj, sec in samples:
                f.write(f"{ts},{maj},{sec},100\n")


def write_pair(sdir, n, rev="r1", ctl=None, trt=None):
    p = os.path.join(sdir, f"pair-{n:02d}")
    write_run(os.path.join(p, "control"), **(ctl or {}))
    write_run(os.path.join(p, "treatment"), **(trt or {}))
    json.dump({"scenario_rev": rev, "order": "control treatment", "attempts": 1}, open(os.path.join(p, "pair.done"), "w"))
    return p


class SyntheticTree(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.r = os.path.join(self.t, "results")
        s3a = os.path.join(self.r, "S3a")
        for n in (1, 2):
            write_pair(s3a, n,
                       ctl={"took": "10.5s", "sha": "C", "samples": [(1000, 0, 0), (1010, 0, 0), (1090, 8000, 2_000_000), (1100, 8000, 2_000_000)]},
                       trt={"took": "1.2s", "sha": "T", "samples": [(1000, 0, 0), (1010, 0, 0), (1090, 400, 300_000), (1100, 400, 300_000)]})
        # incomplete pair (no pair.done) and a failed attempt: neither may count as data
        write_run(os.path.join(s3a, "pair-03", "control"), sha="C", took="99s")
        write_run(os.path.join(s3a, "_failed", "pair-02-20261002T000000Z", "control"), status="failed", sha="C", took="99s")
        s1 = os.path.join(self.r, "S1")
        same = {"stm-c16": {"total": 10, "rps": 2000, "p50": 0.002, "p99": 0.010}}
        for n in (1, 2):
            write_pair(s1, n, ctl={"bench": same, "sha": "C", "cap": "infinity"}, trt={"bench": same, "sha": "T", "cap": "infinity"})
        self.config = os.path.join(self.t, "config.env")
        open(self.config, "w").write('PAIRS_S3a="${PAIRS_S3a:-3}"\nPAIRS_S1="${PAIRS_S1:-2}"\n')
        self.versions = os.path.join(self.t, "versions.env")
        open(self.versions, "w").write("SHA_control_etcd=C\nSHA_treatment_etcd=T\n")

    def tearDown(self):
        shutil.rmtree(self.t)

    def test_aggregate_counts_only_done_pairs(self):
        a = aggregate.aggregate(self.r)
        self.assertEqual(a["scenarios"]["S3a"]["n_pairs"], 2)
        s = a["scenarios"]["S3a"]["summary"]
        self.assertAlmostEqual(s["compaction_s"]["control"]["median"], 10.5)
        self.assertAlmostEqual(s["compaction_s"]["ratio"]["median"], 1.2 / 10.5)
        self.assertEqual(s["compaction_majflt"]["control"]["median"], 8000)
        self.assertAlmostEqual(s["compaction_read_mib"]["control"]["median"], 2_000_000 * 512 / 2**20)

    def test_aggregate_equal_reads_ratio_one(self):
        a = aggregate.aggregate(self.r)
        self.assertAlmostEqual(a["scenarios"]["S1"]["summary"]["stm-c16.p99_ms"]["ratio"]["median"], 1.0)

    def check(self, *extra):
        p = subprocess.run([sys.executable, os.path.join(REPORT, "check_complete.py"), self.r, "--config", self.config,
                            "--versions", self.versions, "--scenarios", "S3a S1", *extra], capture_output=True, text=True)
        return p.returncode, p.stdout

    def test_check_flags_missing_and_incomplete_pair(self):
        rc, out = self.check()
        self.assertEqual(rc, 1)
        self.assertIn("S3a: 2/3 pairs done", out)
        self.assertIn("S3a/pair-03: incomplete", out)

    def test_check_passes_when_complete(self):
        shutil.rmtree(os.path.join(self.r, "S3a", "pair-03"))
        open(self.config, "w").write('PAIRS_S3a="${PAIRS_S3a:-2}"\nPAIRS_S1="${PAIRS_S1:-2}"\n')
        rc, out = self.check()
        self.assertEqual(rc, 0, out)

    def test_check_flags_sha_mismatch_and_split_vm(self):
        write_pair(os.path.join(self.r, "S1"), 3, ctl={"sha": "WRONG", "vm": "A"}, trt={"sha": "T", "vm": "B"})
        rc, out = self.check()
        self.assertIn("binary sha does not match", out)
        self.assertIn("different VMs", out)

    def test_check_flags_mixed_scenario_rev(self):
        write_pair(os.path.join(self.r, "S1"), 3, rev="r2", ctl={"sha": "C"}, trt={"sha": "T"})
        _, out = self.check()
        self.assertIn("different scenario_revs", out)


class ValidateS4(unittest.TestCase):
    def run_csv(self, body):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write("type,ratio,conn_size,value_size,iter1,iter2,comment\n" + body)
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "validate_s4.py"), f.name], capture_output=True, text=True)
        os.unlink(f.name)
        return p.returncode

    def test_good(self):
        self.assertEqual(self.run_csv("PARAM,,,,,,x\nDATA,1,32,256,100:50,110:55,\n"), 0)

    def test_zero_cell(self):
        self.assertEqual(self.run_csv("DATA,1,32,256,100:0,110:55,\n"), 1)

    def test_empty_cell(self):
        self.assertEqual(self.run_csv("DATA,1,32,256,,110:55,\n"), 1)

    def test_no_rows(self):
        self.assertEqual(self.run_csv("PARAM,,,,,,x\n"), 1)


class RealOutputs(unittest.TestCase):
    """Parsers against outputs captured on the Mac (P0 step 8)."""
    REAL = os.path.join(REPORT, "testdata", "real")

    def test_bench_files_parse(self):
        files = glob.glob(os.path.join(self.REAL, "*", "pair-01", "*", "bench-*.txt"))
        self.assertTrue(files, "no real bench outputs captured")
        for f in files:
            b = aggregate.parse_bench(f)
            self.assertIsNotNone(b, f)
            self.assertIn("avg_ms", b, f)
            # Smoke-scaled steps can be too short for a printed p99; p50 is always there
            # unless the step is a 1-2 request list.
            if "list" not in os.path.basename(f):
                self.assertIn("p50_ms", b, f)
            self.assertFalse(b["errors"], f)

    def test_compaction_lines_parse(self):
        logs = glob.glob(os.path.join(self.REAL, "S3a", "pair-01", "*", "etcd.log"))
        self.assertTrue(logs)
        for f in logs:
            c = aggregate.parse_compactions(f)
            self.assertEqual(len(c), 1, f)
            self.assertGreater(c[0]["took_s"], 0)
            self.assertIsNotNone(c[0]["end_ts"])

    def test_metrics_histograms_parse(self):
        files = glob.glob(os.path.join(self.REAL, "*", "pair-01", "*", "metrics-end.txt"))
        self.assertTrue(files)
        h = aggregate.prom_hist(files[0], "etcd_disk_wal_fsync_duration_seconds")
        self.assertIn(float("inf"), h)

    def test_s4_csv_parse(self):
        csvs = glob.glob(os.path.join(self.REAL, "S4", "pair-01", "*", "result-*.csv"))
        if not csvs:
            self.skipTest("no real S4 CSV captured yet")
        cells = aggregate.parse_s4_csv(csvs[0])
        self.assertTrue(cells)
        for c in cells.values():
            self.assertGreater(c["read_qps"], 0)

    def test_go_duration(self):
        self.assertAlmostEqual(aggregate.go_duration("1m2.5s"), 62.5)
        self.assertAlmostEqual(aggregate.go_duration("922.88ms"), 0.92288)
        self.assertAlmostEqual(aggregate.go_duration("850µs"), 0.00085)


@unittest.skipUnless(__import__("importlib").util.find_spec("matplotlib"), "matplotlib not installed")
class Render(unittest.TestCase):
    def test_render_synthetic(self):
        t = tempfile.mkdtemp()
        try:
            st = SyntheticTree()
            st.setUp()
            agg = aggregate.aggregate(st.r)
            ap = os.path.join(t, "agg.json")
            json.dump(agg, open(ap, "w"), default=str)
            p = subprocess.run([sys.executable, os.path.join(REPORT, "render.py"), ap, "--out", t, "--draft"],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            for f in ("REPORT.md", "report.html", "GITHUB-COMMENT.md", "charts/s3a-compaction.png"):
                self.assertTrue(os.path.exists(os.path.join(t, f)), f)
            self.assertIn("S3: True", p.stdout)
            self.assertIn("S1: True", p.stdout)
            html_text = open(os.path.join(t, "report.html")).read()
            self.assertIn("<svg", html_text)
            self.assertIn("prefers-color-scheme: dark", html_text)
            st.tearDown()
        finally:
            shutil.rmtree(t)


class RenderHelpers(unittest.TestCase):
    def setUp(self):
        import render
        self.render = render

    def test_rate_limited_stm_latency_left_out(self):
        r = {"ratio": {"median": 1.0, "min": 1.0, "max": 1.0, "n": 1}}
        ms = self.render.read_metrics({"mixed-stm.p99_ms": r, "mixed-stm.rps": r, "stm-background.avg_ms": r,
                                       "stm-c64.p99_ms": r, "mixed-range500.p99_ms": r})
        self.assertEqual(sorted(ms), ["mixed-range500.p99_ms", "mixed-stm.rps", "stm-c64.p99_ms"])

    def test_gib(self):
        self.assertEqual(self.render.gib(5466398720), "5.09 GiB")
        self.assertEqual(self.render.gib("5466398720"), "5.09 GiB")
        for none in ("infinity", float("inf"), None):
            self.assertEqual(self.render.gib(none), "none")


if __name__ == "__main__":
    unittest.main()
