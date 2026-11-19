"""hyperloop.py and its place in the ci-failures flow, offline.

    python test/hyperloop_test.py

A fake alimonitor (fake_alimonitor.py) serves REAL test folders, trimmed
(fixtures/hyperloop: 00809001 passed, 00808999 failed with "Input feature ...
not available"), and a synthetic 00809000: the same failure, other wagons, a
grid user's dataset. Checks: listings, the reductions, the sync's incremental
reads (unchanged folders, a changed one, a test without status yet, one given
up on), the rows, the flow's plan() (tests grouped, Hyperloop errors among the
signatures to embed), and the publish step (a user's login hidden, no error
text, fingerprints kept).
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
WORK = Path(tempfile.mkdtemp(prefix="coco-hl-"))
os.environ["HL_CACHE_DB"] = str(WORK / "logs.db")
import hyperloop as H  # noqa: E402
import logscan  # noqa: E402

failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---- the newest id, from status codes alone
class Ids:
    """A stand-in client: folders exist for ids lo..hi (consecutive)."""
    def __init__(self, lo, hi):
        self.lo, self.hi, self.requests = lo, hi, 0

    def exists(self, path):
        self.requests += 1
        return self.lo <= int(path.rstrip("/").split("/")[-1]) <= self.hi


for start, hi in ((809001, 809001), (809001, 809537), (809001, 830000), (800000, 809001)):
    c = Ids(700000, hi)
    got = H.newest_id(c, start)
    check(got == hi and c.requests <= 2 * (hi - start + 2).bit_length() + 2,
          f"newest id: doubling then bisection from {start} finds {hi} ({got}, {c.requests} requests)")
c = Ids(805000, 809537)
check(H.newest_id(c, 801000) == 809537, f"newest id: from a seed alimonitor no longer keeps ({c.requests} requests)")
check(H.newest_id(Ids(5, 4), 801000) is None, "newest id: nothing there, None")
check(H.folder(808999) == "0080/00808999/", "a test's folder: <id // 10000>/<id, 8 digits>/")
check(H.device_of("[2539214:hf-correlator-dplus-dplus-reduced]: [11:13:53][FATAL] x") ==
      "hf-correlator-dplus-dplus-reduced" and H.device_of("ERROR: O2 exit code is 1") is None,
      "the DPL device of a log line")
check(H.device_of("", "crash in hf-task-flow (o2-analysis-hf-task-flow): f < g") == "hf-task-flow"
      and H.device_of("[ERROR] SEVERE: Device tof-pid (123) had at least one") == "tof-pid"
      and H.device_of("[ERROR]  - Device tof-pid: pid 123 (exit 128)") == "tof-pid"
      and H.device_of("[ERROR] Workflow crashed - PID 9 (tof-pid) did not exit correctly") == "tof-pid",
      "the device of a crash signature and of the driver's own reports")
FIX = HERE / "fixtures" / "hyperloop" / "tests" / "0080"
st, devs = H.reduce_status(json.loads((FIX / "00809001" / "status.json").read_text()))
check(st["test_exitcode"] == "done" and "device_metrics" not in st and devs and
      all({"workflow", "device", "cpu_s", "pss_bytes"} <= set(d) for d in devs),
      f"status.json: scalars, and one row per device ({len(devs)})")
cfg = H.reduce_config(json.loads((FIX / "00809001" / "full_config.json").read_text()))
check(cfg["package_tag"].endswith("daily-20261118-1200-1") and cfg["dataset"].startswith("/alice/data/")
      and cfg["wagons"] and "configuration" not in json.dumps(cfg), "full_config.json: tag, dataset, wagons; no configurables")

# ---- sync against a fake alimonitor
root = WORK / "site"
shutil.copytree(HERE / "fixtures" / "hyperloop", root)
now = time.time()
for p in sorted(root.rglob("*"), reverse=True):
    os.utime(p, (now - 600, now - 600))
os.utime(root / "tests", (now - 600, now - 600))
port, req_log = free_port(), WORK / "requests.log"
srv = subprocess.Popen([sys.executable, str(HERE / "fake_alimonitor.py"), str(port), str(root), str(req_log)])
time.sleep(0.5)
base = f"http://127.0.0.1:{port}/tests/"
extract = lambda raw: logscan.extract_log(raw, 40, 1500)  # noqa: E731
try:
    db = sqlite3.connect(WORK / "logs.db", isolation_level=None)

    def run(**kw):
        req_log.write_text("")
        s = H.sync(db, extract, logscan.EXTRACT_VERSION, log=lambda *a: None,
                   client=H.Client(base=base, rate=0, cafile=None), **dict(dict(budget_s=60), **kw))
        return s, req_log.read_text().split("\n")[:-1]

    s, reqs = run()
    check(s["read"] == 3 and s["failed"] == 2, f"first pass: 3 tests read, 2 failed ({s})")
    got = {r[0]: r[1:] for r in db.execute("SELECT test_id, errors IS NOT NULL, perf IS NOT NULL FROM hl_test")}
    check(got == {808999: (1, 0), 809000: (1, 0), 809001: (0, 0)}, f"logs read for failed tests only ({got})")
    check(not any("profile" in r or ".root" in r or "performanceMetrics" in r for r in reqs),
          "no profile, ROOT file or metrics file fetched (the metrics file: the hyperloop-perf flow)")
    check(s["newest"] == 809001 and any("00808998" in r for r in reqs), "first pass: down from the newest id to the bottom")
    s, reqs = run()
    check(s["read"] == 0 and all(r.startswith("HEAD ") for r in reqs) and len(reqs) <= 3,
          f"second pass: a test is read once; only the search for the newest id ({reqs})")
    (root / "tests" / "0080" / "00809002").mkdir()
    (root / "tests" / "0080" / "00809002" / "stdout.log").write_text("running\n")
    floor = json.loads(db.execute("SELECT value FROM hl_state WHERE key = 'floor'").fetchone()[0])
    check(floor["id"] == 808998, f"the bottom of alimonitor's history is remembered ({floor})")
    db.execute("UPDATE hl_state SET value = ? WHERE key = 'floor'",
               (json.dumps(dict(floor, at=time.time() - H.FLOOR_TTL_S - 1)),))
    s, reqs = run()
    check(any("00808998/" in r for r in reqs), "the remembered bottom expires, and is probed again")
    s, reqs = run()
    check(not any("00808998/" in r for r in reqs), "then remembered again")
    s, reqs = run()
    pend = db.execute("SELECT status, gave_up FROM hl_test WHERE test_id = 809002").fetchone()
    check(s["newest"] == 809002 and s["no_status"] == 1 and pend == (None, 0),
          f"a new test without status.json yet: found, kept pending ({pend})")
    s, reqs = run()
    check(any("00809002/status.json" in r for r in reqs) and s["no_status"] == 1, "a pending test is asked again")
    db.execute("UPDATE hl_test SET first_seen = '2020-01-01T00:00:00Z' WHERE test_id = 809002")
    s, _ = run()
    gave = db.execute("SELECT gave_up FROM hl_test WHERE test_id = 809002").fetchone()
    s2, reqs2 = run()
    check(gave == (1,) and not any("00809002/status.json" in r for r in reqs2),
          "a test still without status a day after it was first seen is given up on, and not asked again")

    # ---- rows
    db.row_factory = sqlite3.Row
    ok = H.rows(dict(db.execute("SELECT * FROM hl_test WHERE test_id = 809001").fetchone()))
    bad = H.rows(dict(db.execute("SELECT * FROM hl_test WHERE test_id = 808999").fetchone()))
    check(ok["test"]["state"] == "success" and ok["test"]["tag"].startswith("O2Physics::") and ok["test"]["max_pss_kb"]
          and ok["test"]["cpu_ms"] and not ok["errors"], "passed test: state, tag, largest PSS, CPU, no errors")
    devm = [m for m in ok["metrics"] if m["device_id"]]
    check(devm and len(devm) == len(ok["metrics"]) and any(m["wagon_id"] for m in devm),
          f"metrics: per device (CPU, PSS, with its wagon), nothing per workflow here: {len(devm)}")
    whole = [v for m in ok["metrics"] for k, v in m.items() if v is not None]
    check(all(isinstance(v, int) for v in whole), "compact: every metric value a whole number (ids, kB, ms, counts)")
    check(not {"end_time", "tag", "state", "workflow", "device", "sig_exact", "sig_class"}
          & {k for m in ok["metrics"] + bad["errors"] for k in m}
          and not {"folder_url", "log_url"} & set(ok["test"]), "compact: no copies of the test's fields, no URLs")
    names = {**ok["names"], **bad["names"]}
    check(all(H.name_id(n) == i for i, n in names.items()) and all(m["workflow_id"] in names for m in ok["metrics"])
          and all(m["device_id"] in names for m in devm), f"names: every id used has its name ({len(names)})")
    devices = {names.get(e["device_id"]) for e in bad["errors"]}
    nodev = [e["sig_hash"] for e in bad["errors"] if e["device_id"] is None]
    check(len(nodev) <= 2, f"failed test: only the run's summary lines have no device ({nodev})")
    check(bad["test"]["state"] == "failure" and bad["test"]["n_wagons"] > 0 and "hf-correlator-dplus-dplus-reduced" in devices
          and any(e["fatal"] for e in bad["errors"]), f"failed test: its errors, with the device that printed them ({devices})")
    check(all(m["wagon_id"] is None or isinstance(m["wagon_id"], int) for m in ok["metrics"]), "wagon ids are integers")

    # ---- the flow's plan(): tests grouped, their errors the signatures to embed
    import hyperloop_flow as F
    hl, groups, name_groups = F.plan()
    check({i for g in name_groups.values() for i, _ in g} >= set(names), "plan: the names the rows use")
    sigs = {s[0] for g in groups.values() for s in g}
    check(sum(map(len, hl.values())) == 3 and 808 in hl and 809 in hl, f"plan: 3 tests in groups by thousand ({dict(hl)})")
    check({e["sig_hash"] for e in bad["errors"]} <= sigs, "plan: the failed tests' errors are signatures to embed")
    # ---- the size cap: the newest HL_MAX_TESTS folders, older ones dropped
    s, reqs = run(max_tests=2)
    left = {r[0] for r in db.execute("SELECT test_id FROM hl_test")}
    check(s["walked"] == 2 and left == {809002, 809001} and s["pruned"] == 2,
          f"cap: only the newest 2 folders are kept, the older tests leave the cache ({s['pruned']} dropped, {left})")
    check(not any("00808999" in r or "00809000" in r for r in reqs), "cap: tests beyond it are not fetched")
    t, _, _ = F.plan()
    check({tid for g in t.values() for tid, _ in g} == {809001}, "cap: the dropped tests leave the flow's plan")
    check(H.prune(db, 1) == 1 and H.prune(db, 1) == 0, "prune: keeps the newest N, idempotent")
    # ---- the daily summary: recorded on reading, kept after the test leaves the cache
    days = dict(db.execute("SELECT test_id, state FROM hl_day_test").fetchall())
    pairs = db.execute("SELECT count(*), count(DISTINCT sig_hash) FROM hl_day_sig").fetchone()
    nsig = db.execute("SELECT count(*) FROM hl_sig").fetchone()[0]
    check(days.get(808999) == "failure" and days.get(809001) == "success" and pairs[0] > 0 and nsig == pairs[1],
          f"summary: every test read has its day; failed tests their signatures, text once ({pairs}, {nsig})")
    db.execute("INSERT INTO hl_day_test VALUES (1, '2020-01-01', 't', 'failure', '[1]')")
    db.execute("INSERT INTO hl_day_sig VALUES (1, 'oldsig')")
    db.execute("INSERT INTO hl_sig VALUES ('oldsig', 'c', 'k', 0, 'x', 'x', NULL)")
    check(H.prune_days(db, 180) == 1 and not db.execute("SELECT 1 FROM hl_sig WHERE sig_hash = 'oldsig'").fetchone()
          and db.execute("SELECT count(*) FROM hl_sig").fetchone()[0] == nsig,
          "summary: days older than HL_SUMMARY_DAYS go, with the signatures no day names")

    # ---- stopping at a size: the walk adds up the index bytes of each test
    s, _ = run()                                    # all 3 back in the cache
    # as the walk counts them: the detailed rows only (signatures last as long as the daily summary)
    sizes = {r[0]: H.est_parts(dict(r))[0]
             for r in db.execute("SELECT * FROM hl_test WHERE status IS NOT NULL ORDER BY test_id DESC")}
    check(all(v > 0 for v in sizes.values()), f"est_bytes: every test with a status has a size ({sizes})")
    newest2 = sizes[809001] + sizes[809000]
    # 809002 (no status, 0 B), 809001, 809000 fill the budget and its slack
    s, reqs = run(max_bytes=newest2 / (1 + H.SLACK) * 0.999)
    left = {r[0] for r in db.execute("SELECT test_id FROM hl_test WHERE status IS NOT NULL")}
    check(s["full"] and left == {809001, 809000} and s["pruned"] == 1,
          f"size: the walk stops once the budget is filled, the older test leaves the cache ({s['est_mb']} MB, {left})")
    # Filled at 809001 + 809000; 808999 only fits in the slack: kept if cached, never fetched
    s, reqs = run(max_bytes=newest2 / (1 + H.SLACK / 2))
    check(not any("00808999" in r for r in reqs), "size: once full, older tests are not fetched")
    s, _ = run()                                    # back to all three
    H.SLACK, slack = sizes[808999] / newest2 * 1.5, H.SLACK   # 808999 inside the slack of the budget
    s, reqs = run(max_bytes=newest2)
    H.SLACK = slack
    left = {r[0] for r in db.execute("SELECT test_id FROM hl_test WHERE status IS NOT NULL")}
    check(left == {809001, 809000, 808999} and s["pruned"] == 0 and s["read"] == 0,
          f"size: a cached test just past the budget stays (slack), not dropped and fetched again ({left})")
    s, _ = run(max_bytes=newest2 / (1 + H.SLACK) * 0.999, scale=1000)
    left = {r[0] for r in db.execute("SELECT test_id FROM hl_test WHERE status IS NOT NULL")}
    check(left == {809001}, f"size: the scale from the real DB counts ({left})")
    # ---- the estimates are stored with each test, and computed once
    run()
    stored = dict(db.execute("SELECT test_id, est_version FROM hl_test WHERE status IS NOT NULL").fetchall())
    check(set(stored.values()) == {H.EST_VERSION}, f"est: stored with every test when it is read ({stored})")
    calls, real_rows = [0], H.rows
    H.rows = lambda r: (calls.__setitem__(0, calls[0] + 1), real_rows(r))[1]
    s, _ = run(max_bytes=1e9)
    check(calls[0] == 0, f"est: a walk over cached tests does not rebuild rows ({calls[0]} calls)")
    db.execute("UPDATE hl_test SET reduce_version = 0 WHERE test_id = 808999")   # as after a REDUCE_VERSION bump
    s, _ = run(max_bytes=1e9)
    H.rows = real_rows
    check(calls[0] == 1, f"est: a test read again is estimated again, once ({calls[0]} rows() call)")
    run()
    t = {r[0]: H.test_cost(db, r[0], set()) for r in db.execute("SELECT test_id FROM hl_test WHERE status IS NOT NULL")}
    u = {r[0]: H.est_bytes(dict(r)) for r in db.execute("SELECT * FROM hl_test WHERE status IS NOT NULL")}
    check(t == u, "est: the stored estimate is the computed one")
    cache = sqlite3.connect(WORK / "logs.db")
    check(F.scale(cache, WORK / "missing.db")[0] == F.DEFAULT_SCALE, "scale: nothing indexed yet, the default")
    cache.close()

    s, _ = run()   # a larger cap: the window reaches back again, and they are read again
    back = {r[0] for r in db.execute("SELECT test_id FROM hl_test WHERE status IS NOT NULL")}
    check(back == {809001, 809000, 808999},
          f"no limit reached: the window reaches back again, the dropped tests read again ({s['read']} read)")
    db.close()

    # ---- publishing: a copy of the flow's tables, sanitized
    import sanitize
    pub = WORK / "hyperloop.db"
    c = sqlite3.connect(pub)
    for t, cls in (("hl_tests_data", F.HLTest), ("hl_test_wagons_data", F.HLTestWagon),
                   ("hl_metrics_data", F.HLMetric), ("hl_errors_data", F.HLError), ("hl_signatures", F.HLSignature),
                   ("hl_names", F.HLName), ("hl_meta", F.HLMeta), ("hl_extra", F.HLMeta),
                   ("hl_error_days", F.HLErrorDay), ("hl_days", F.HLDay)):
        cols = list(cls.__dataclass_fields__)
        c.execute(f"CREATE TABLE {t} ({', '.join(cols)})")
    src = sqlite3.connect(WORK / "logs.db")
    src.row_factory = sqlite3.Row
    for r in src.execute("SELECT * FROM hl_test WHERE status IS NOT NULL"):
        x = H.rows(dict(r))
        x["names"] = [dict(name_id=i, name=n) for i, n in x["names"].items()]
        for t, rows_ in (("hl_tests_data", [x["test"]]), ("hl_test_wagons_data", x["wagons"]),
                         ("hl_metrics_data", x["metrics"]), ("hl_errors_data", x["errors"]), ("hl_names", x["names"])):
            for row in rows_:
                c.execute(f"INSERT OR IGNORE INTO {t} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                          list(row.values()))
    c.execute("INSERT INTO hl_meta VALUES ('base', ?)", (base,))
    _, groups_, _ = F.plan()   # the signatures, as the flow declares them (text once, a vector of the real size)
    for g in groups_.values():
        for h, cls_h, kind, infra, exact, cls, file in g:
            c.execute("INSERT INTO hl_signatures (sig_hash, class_hash, kind, infra, sig_exact, sig_class, file, "
                      "embedding) VALUES (?,?,?,?,?,?,?,?)", (h, cls_h, kind, int(infra), exact, cls, file or None,
                                                              bytes(3072)))
    c.commit()
    c.close()
    stats = sanitize.sanitize_hl_db(pub, templates=None)
    c = sqlite3.connect(pub)
    ds = dict(c.execute("SELECT test_id, dataset FROM hl_tests_data").fetchall())
    check(ds[809000] == "/alice/cern.ch/user/_/_/derived/run3" and ds[809001].startswith("/alice/data/"),
          f"publish: a grid user's login in a dataset is hidden, data paths kept ({ds[809000]})")
    cols = {r[1] for r in c.execute("PRAGMA table_info(hl_errors_data)")}
    nfp = c.execute("SELECT count(*) FROM hl_errors_data e JOIN hl_signatures s USING (sig_hash) "
                    "WHERE e.fingerprint IS s.fingerprint AND e.fingerprint_kind IS s.fingerprint_kind").fetchone()[0]
    check("sig_exact" not in cols and "sig_class" not in cols and "fingerprint" in cols
          and nfp == c.execute("SELECT count(*) FROM hl_errors_data").fetchone()[0],
          f"publish: no error text in hl_errors, each with its signature's fingerprint ({nfp})")
    scols = {r[1] for r in c.execute("PRAGMA table_info(hl_signatures)")}
    check("sig_exact" not in scols and "embedding" in scols and "fingerprint" in scols,
          "publish: hl_signatures keeps the embedding and the fingerprint, not the text")
    n = c.execute("SELECT count(*) FROM hl_errors_data").fetchone()[0]
    check(not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'hl_extra'").fetchone(),
          "publish: a table not on the list is dropped")
    # the reader's views give the readable shape back
    import reader
    for ddl in reader.INDEXES["hyperloop.db"]:
        c.execute(ddl)
    m = c.execute("SELECT workflow, device, cpu_s, pss_bytes, tag, end_time FROM hl_metrics "
                  "WHERE test_id = 809001 AND device != '' ORDER BY pss_bytes DESC LIMIT 1").fetchone()
    t = c.execute("SELECT folder_url, log_url, max_pss_bytes FROM hl_tests WHERE test_id = 809001").fetchone()
    e = c.execute("SELECT device, tag, state FROM hl_errors WHERE test_id = 808999 AND device IS NOT NULL").fetchone()
    check(m[0] and m[1] and m[2] > 0 and m[3] == t[2] and m[4] and m[5] and t[0] == base + "0080/00809001/"
          and t[1].endswith("/stdout.log") and e[0] and e[2] == "failure",
          f"views: names, bytes, seconds, the test's tag and time, URLs back ({m}, {t}, {e})")
    check(n > 0 and stats.get("hl_tests: user in dataset hidden") == 1, f"publish: {n} Hyperloop errors kept, by hash")
    c.close()
finally:
    srv.terminate()

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
