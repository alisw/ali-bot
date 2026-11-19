"""Offline tests of hlperf.py and the hyperloop-perf flow's plan, publishing and
views: a synthetic performanceMetrics_processed.json shaped as the real ones
(values as strings, ~5 s samples, table-bytes series), and the fixture folders
served by test/fake_alimonitor.py.

    <venv>/bin/python test/hlperf_test.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent / "ci-mcp"))
WORK = Path(tempfile.mkdtemp(prefix="coco-hp-"))
os.environ["HP_CACHE_DB"] = str(WORK / "perf.db")
import hlperf as P  # noqa: E402
import hyperloop as H  # noqa: E402

failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


def ser(vals, t0=1795106832000, dt=5000):
    return [{"timestamp": str(t0 + i * dt), "value": f"{v:.6f}"} for i, v in enumerate(vals)]


# ---- reduction
pss = [100000 + 1000 * i for i in range(60)]              # kB, growing 1000 kB per 5 s = 12000 kB/min
synthetic = {
    "o2-analysis-hf-producer": {
        "wagon_id": 4242, "proportionalSetSize": ser(pss), "memoryPrivateDirty": ser([50000] * 59 + [90000]),
        "residentSetSize": ser([300000] * 60), "virtualMemorySize": ser([9000000] * 60),
        "cpuUsedAbsolute": ser([5000000] * 60),             # 5 s of CPU per 5 s sample: one core
        "cpuUsedPercentage": ser([100.0] * 57 + [150.0] * 3), "cpuInstructions": ser([1e9] * 60),
        "ccdb-cache-hit": 3.0, "ccdb-cache-miss": 1.0, "ccdb-cache-failure": 0.0,
        "ccdb-cache-fetched-bytes": 7214.0, "ccdbQueries": 4,
        "table-bytes-AOD-HFCAND3PBASE-0-created": ser([0, 1e6, 3e6]),
        "table-bytes-AOD-HFSELLC-0-created": ser([0, 5e5]),
        **{f"table-bytes-AOD-T{i}-0-created": ser([i * 1000.0]) for i in range(1, 8)},
        "proportionalSetSize_summary": {"avg": 1.0, "slope": 1.0, "max": 1.0}},
    "reader": {"wagon_id": None, "proportionalSetSize": ser([20000] * 10), "cpuUsedAbsolute": ser([1e6] * 10)},
    "__full_train__": {"processed_size": 1901102956.65, "processed_files": 2, "timeframes_consumed": ser([10, 20, 41]),
                       "ccdb_time": 43, "total_ccdb_time": 62, "proportionalSetSize": ser([1e6, 2e6, 2.5e6]),
                       "memoryPrivateDirty": ser([1e6, 2e6]), "cpuUsedAbsolute": ser([0, 3e6, 2e6])},
    "AOD-HFLCBASE-0": {"table-bytes-per-table": ser([1216, 1072])},
}
r = P.reduce(synthetic)
w = r["workflows"]["o2-analysis-hf-producer"]
check(set(r["workflows"]) == {"o2-analysis-hf-producer", "reader"}, "reduce: per workflow, no AOD-* input entries")
check(w["pss"]["max"] == pss[-1] and w["pss"]["med"] == pss[30] and w["pss"]["p95"] == pss[57]
      and abs(w["pss"]["mean"] - sum(pss) / 60) < 1e-6, f"reduce: PSS mean, median, p95, max of the series ({w['pss']})")
check(abs(w["pss_slope"] - 12000) < 1, f"reduce: PSS growth per minute, least squares ({w['pss_slope']:.1f} kB/min)")
check(w["dirty"]["max"] == 90000 and w["dirty"]["med"] == 50000, "reduce: private dirty memory, its own statistics")
check(w["cpu_us"] == 60 * 5e6 and w["cpu_pct"]["med"] == 100.0 and w["cpu_pct"]["p95"] == 150.0
      and w["instructions"] == 60e9, "reduce: CPU summed (per-interval microseconds), CPU % median and p95, instructions")
check(w["top_tables"][0] == ("HFCAND3PBASE-0", 3e6) and len(w["top_tables"]) == P.TOP_TABLES
      and w["out_tables"] == 9 and w["out_bytes"] == 3e6 + 5e5 + sum(i * 1000.0 for i in range(1, 8)),
      f"reduce: output tables, the largest {P.TOP_TABLES} by their final size, and the total ({w['top_tables'][:2]})")
check(w["ccdb_bytes"] == 7214 and w["ccdb_queries"] == 4 and w["wagon_id"] == 4242, "reduce: CCDB counters, wagon")
tr = r["train"]
check(tr["processed_bytes"] == 1901102956.65 and tr["timeframes"] == 41 and tr["cpu_us"] == 5e6
      and tr["pss"]["max"] == 2.5e6, f"reduce: the whole train ({tr['timeframes']} time frames)")
check(len(json.dumps(r)) < 3000, f"reduce: a few kB instead of the file's size ({len(json.dumps(r))} B)")

row = {"test_id": 1, "state": "read", "perf": json.dumps(r)}
x = P.rows(row)
wr = [y for y in x["workflows"] if y["wagon_id"] == 4242][0]
check(wr["pss_max_kb"] == pss[-1] and wr["cpu_ms"] == 300000 and wr["cpu_pct_med_x10"] == 1000
      and all(isinstance(v, int) for v in wr.values() if v is not None),
      "rows: whole numbers (kB, ms, tenths of a percent)")
check(len(x["tables"]) == P.TOP_TABLES and all(t["table_id"] in x["names"] for t in x["tables"])
      and x["names"][wr["workflow_id"]] == "o2-analysis-hf-producer", "rows: tables and workflows by name id")
check(P.rows({"test_id": 2, "state": "none", "perf": None})["test"] is None, "rows: a test without metrics, none")
n, names = P.est(row)
check(500 < n < 3000 and names == x["names"], f"est: {n:.0f} B for the test's rows")

# ---- sync against a fake alimonitor (the fixture folders: 809001 passed, 808999 and 809000 failed)
root = WORK / "site"
shutil.copytree(HERE / "fixtures" / "hyperloop", root)
s_ = __import__("socket").socket()
s_.bind(("127.0.0.1", 0))
port = s_.getsockname()[1]
s_.close()
req_log = WORK / "requests.log"
srv = subprocess.Popen([sys.executable, str(HERE / "fake_alimonitor.py"), str(port), str(root), str(req_log)])
time.sleep(0.5)
base = f"http://127.0.0.1:{port}/tests/"
try:
    db = sqlite3.connect(WORK / "perf.db", isolation_level=None)

    def run(**kw):
        req_log.write_text("")
        st = P.sync(db, log=lambda *a: None, client=H.Client(base=base, rate=0, cafile=None),
                    **dict(dict(budget_s=60), **kw))
        return st, req_log.read_text().split("\n")[:-1]

    st, reqs = run()
    got = dict(db.execute("SELECT test_id, state FROM hp_test").fetchall())
    check(got == {809001: "read", 809000: "none", 808999: "none"} and st["read"] == 1 and st["no_metrics"] == 2,
          f"sync: the passed test's metrics read, the failed ones marked without ({got})")
    check(not any(("status.json" in q or "stdout" in q) and "00809001" in q for q in reqs)
          and not any("stdout" in q or "full_config" in q for q in reqs),
          "sync: only the metrics file (status.json only where it is missing), never the logs or the config")
    st, reqs = run()
    check(st["read"] == 0 and all(q.startswith("HEAD ") for q in reqs), f"sync: a test is read once ({len(reqs)} requests)")
    (root / "tests" / "0080" / "00809002").mkdir()
    st, _ = run()
    check(dict(db.execute("SELECT test_id, state FROM hp_test").fetchall()).get(809002) == "pending",
          "sync: a test not finished is pending, asked again")
    est = db.execute("SELECT est_bytes, est_version FROM hp_test WHERE test_id = 809001").fetchone()
    check(est[0] > 0 and est[1] == P.EST_VERSION, f"sync: the estimate stored with the test ({est[0]:.0f} B)")
    st, _ = run(max_bytes=1.0)
    check(st["full"], "sync: a size budget stops the walk")

    # ---- the flow's plan, publishing and the readers' views
    import hyperloop_perf_flow as F
    tests, names = F.plan()
    check([t for g in tests.values() for t, _ in g] == [809001], "plan: only tests with metrics")
    pub = WORK / "hyperloop-perf.db"
    c = sqlite3.connect(pub)
    for t, cls in (("hp_tests_data", F.HPTest), ("hp_workflows_data", F.HPWorkflow), ("hp_tables_data", F.HPTable),
                   ("hp_names", F.HPName), ("hp_extra", F.HPName)):
        c.execute(f"CREATE TABLE {t} ({', '.join(cls.__dataclass_fields__)})")
    real_row = dict(zip(("test_id", "state", "perf"),
                        db.execute("SELECT test_id, state, perf FROM hp_test WHERE test_id = 809001").fetchone()))
    for rowd in (row, real_row):
        x = P.rows(rowd)
        for t, rs in (("hp_tests_data", [x["test"]]), ("hp_workflows_data", x["workflows"]),
                      ("hp_tables_data", x["tables"]),
                      ("hp_names", [dict(name_id=i, name=n) for i, n in x["names"].items()])):
            for rr in rs:
                c.execute(f"INSERT OR IGNORE INTO {t} ({', '.join(rr)}) VALUES ({', '.join('?' * len(rr))})",
                          list(rr.values()))
    c.commit()
    c.close()
    import sanitize
    sanitize.sanitize_hp_db(pub)
    c = sqlite3.connect(pub)
    check(not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'hp_extra'").fetchone(),
          "publish: a table not on the list is dropped")
    import reader
    for ddl in reader.INDEXES["hyperloop-perf.db"]:
        c.execute(ddl)
    v = c.execute("SELECT workflow, wagon_id, pss_max_mb, pss_slope_mb_min, cpu_s, cpu_pct_med, out_mb FROM hp_workflows "
                  "WHERE test_id = 1 AND wagon_id = 4242").fetchone()
    t = c.execute("SELECT workflow, output_table, mb FROM hp_tables WHERE test_id = 1 ORDER BY mb DESC").fetchone()
    real = c.execute("SELECT count(*), max(pss_max_mb) FROM hp_workflows WHERE test_id = 809001").fetchone()
    check(v == ("o2-analysis-hf-producer", 4242, round(pss[-1] / 1024, 1), round(12000 / 1024, 2), 300.0, 100.0,
                round(w["out_bytes"] / 1048576, 1)), f"views: names, MB, seconds, percent ({v})")
    check(t == ("o2-analysis-hf-producer", "HFCAND3PBASE-0", round(3e6 / 1048576, 2)), f"views: output tables ({t})")
    check(real[0] >= 8 and real[1] > 0, f"views: the fixture test's workflows ({real})")
finally:
    srv.terminate()

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
