"""Hyperloop wagon tests' resource metrics, for the hyperloop-perf flow
(hyperloop_perf_flow.py, HYPERLOOP_TESTS.md): one file per passed test,

    <HL_BASE><id // 10000, 4 digits>/<id, 8 digits>/performanceMetrics_processed.json

reduced to statistics per workflow, so memory and CPU can be compared across
wagons, tags and datasets, and the expensive wagons found. The hyperloop flow
(hyperloop.py) reads the same folders for the errors; the two flows share the
walk and the HTTP client (hyperloop.walk, hyperloop.Client) but not their
files, so each is re-extracted (HP_REDUCE_VERSION here) on its own.

What the file holds (O2 Monitoring, ProcessMonitor.cxx; a sample every ~5 s per
workflow, values as strings):

    proportionalSetSize    PSS, kB (smaps_rollup): shared pages split among sharers
    memoryPrivateDirty     kB written by this process alone: its own heap
    residentSetSize        RSS, kB (shared pages counted in full)
    virtualMemorySize      kB of address space
    cpuUsedAbsolute        CPU microseconds in the interval (user + system, children
                           included): the sum is the total
    cpuUsedPercentage      the same over the interval's time: 100 = one core busy
    cpuInstructions        retired instructions in the interval (newer tags only)
    table-bytes-AOD-<T>-created   bytes of output table T the workflow wrote so far
    ccdb-cache-*, ccdbQueries     CCDB cache counters
    __full_train__         the whole train: input processed, time frames, CCDB time
    AOD-<T>                input table sizes (not kept)

    HP_TOP_TABLES          output tables kept per workflow, the largest (5)
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import hyperloop as H

TOP_TABLES = int(os.environ.get("HP_TOP_TABLES", "5"))
# Bump when reduce() changes: every cached test is fetched again (this flow's
# files only). Bump ROWS_VERSION when rows() changes (no fetching).
REDUCE_VERSION = 1
ROWS_VERSION = 1
EST_VERSION = ROWS_VERSION * 1000 + 1

SCHEMA = """
  CREATE TABLE IF NOT EXISTS hp_test(        -- one per Hyperloop test (hlperf.py)
    test_id INTEGER PRIMARY KEY,
    state TEXT,             -- read | none (failed, or no metrics file) | pending (no status.json yet)
    perf TEXT,              -- JSON: reduce() (state read)
    reduce_version INTEGER, first_seen TEXT, updated_at TEXT,
    est_bytes REAL,         -- est(): bytes of its rows in hyperloop-perf.db
    est_names TEXT,         -- JSON {name_id: name}: the workflow and table names its rows use
    est_version INTEGER);
"""


def ensure_schema(db) -> None:
    db.executescript(SCHEMA)


# ---------------------------------------------------------------- reduction
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def series(v) -> list[tuple[int, float]]:
    """[(ms, value)] of a {timestamp, value} series, in time order, numbers
    (the file has them as strings). A scalar is one point at 0."""
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                t, y = _num(x.get("timestamp")), _num(x.get("value"))
                if t is not None and y is not None:
                    out.append((int(t), y))
        return sorted(out)
    y = _num(v)
    return [] if y is None else [(0, y)]


def _q(vals: list[float], f: float) -> float | None:
    """The f-quantile (nearest rank) of sorted vals."""
    return vals[min(len(vals) - 1, int(f * len(vals)))] if vals else None


def stats(s: list[tuple[int, float]]) -> dict:
    """mean, median, p95, max of a gauge series (memory, CPU %)."""
    v = sorted(y for _, y in s)
    if not v:
        return {"mean": None, "med": None, "p95": None, "max": None}
    return {"mean": sum(v) / len(v), "med": _q(v, 0.5), "p95": _q(v, 0.95), "max": v[-1]}


def slope_per_min(s: list[tuple[int, float]]) -> float | None:
    """Least-squares slope of a series, in its unit per minute: memory that
    keeps growing over a test (a leak, or a cache without bound) has a large one."""
    if len(s) < 3:
        return None
    ts = [t / 60000.0 for t, _ in s]
    ys = [y for _, y in s]
    mt, my = sum(ts) / len(ts), sum(ys) / len(ys)
    var = sum((t - mt) ** 2 for t in ts)
    return sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / var if var else None


def _last(v) -> float | None:
    s = series(v)
    return max(y for _, y in s) if s else None   # cumulative counters: the largest is the final


def reduce(p: dict) -> dict:
    """performanceMetrics_processed.json -> {"workflows": {wf: {...}}, "train": {...}}.
    Memory in kB, CPU in microseconds (summed) and percent (median, p95)."""
    wfs, train = {}, {}
    for wf, m in (p or {}).items():
        if not isinstance(m, dict) or wf.startswith("AOD-"):
            continue
        pss, dirty = series(m.get("proportionalSetSize")), series(m.get("memoryPrivateDirty"))
        cpu_abs, cpu_pct = series(m.get("cpuUsedAbsolute")), series(m.get("cpuUsedPercentage"))
        instr = series(m.get("cpuInstructions"))
        if wf == "__full_train__":
            train = {"processed_bytes": _last(m.get("processed_size")), "processed_files": _last(m.get("processed_files")),
                     "timeframes": _last(m.get("timeframes_consumed")), "ccdb_time": _num(m.get("ccdb_time")),
                     "total_ccdb_time": _num(m.get("total_ccdb_time")), "pss": stats(pss),
                     "dirty_max": stats(dirty)["max"], "cpu_us": sum(y for _, y in cpu_abs) if cpu_abs else None}
            continue
        tables = {}
        for k, v in m.items():
            if k.startswith("table-bytes-AOD-") and k.endswith("-created"):
                b = _last(v)
                if b:
                    tables[k[len("table-bytes-AOD-"):-len("-created")]] = b
        top = sorted(tables.items(), key=lambda x: -x[1])[:TOP_TABLES]
        wfs[wf] = {
            "wagon_id": H._int(m.get("wagon_id")), "n": len(pss), "pss": stats(pss),
            "pss_slope": slope_per_min(pss), "dirty": stats(dirty),
            "rss_max": stats(series(m.get("residentSetSize")))["max"],
            "vms_max": stats(series(m.get("virtualMemorySize")))["max"],
            "cpu_us": sum(y for _, y in cpu_abs) if cpu_abs else None, "cpu_pct": stats(cpu_pct),
            "instructions": sum(y for _, y in instr) if instr else None,
            "ccdb_hit": _num(m.get("ccdb-cache-hit")), "ccdb_miss": _num(m.get("ccdb-cache-miss")),
            "ccdb_fail": _num(m.get("ccdb-cache-failure")), "ccdb_bytes": _num(m.get("ccdb-cache-fetched-bytes")),
            "ccdb_queries": _num(m.get("ccdbQueries")),
            "out_bytes": sum(tables.values()) if tables else None, "out_tables": len(tables), "top_tables": top}
    return {"workflows": wfs, "train": train}


# ---------------------------------------------------------------- reading
def read(db, cl: H.Client, tid: int, stats_: dict) -> str:
    """Read one test into hp_test: "read", "pending" (its folder exists, its
    status.json not yet: asked again, given up a day after first seen),
    "missing" (no folder), or "read" with state none (failed, or no metrics)."""
    f = H.folder(tid)
    p = cl.json(f + "performanceMetrics_processed.json")
    now = H._now()
    if p is not None:
        perf = reduce(p)
        db.execute("INSERT INTO hp_test(test_id, state, perf, reduce_version, first_seen, updated_at) "
                   "VALUES (?, 'read', ?, ?, ?, ?) ON CONFLICT(test_id) DO UPDATE SET state = 'read', "
                   "perf = excluded.perf, reduce_version = excluded.reduce_version, updated_at = excluded.updated_at, "
                   "est_version = NULL", (tid, json.dumps(perf), REDUCE_VERSION, now, now))
        cost(db, tid)
        stats_["read"] += 1
        return "read"
    if cl.json(f + "status.json") is not None:   # finished without metrics: a failed test
        db.execute("INSERT INTO hp_test(test_id, state, reduce_version, first_seen, updated_at) "
                   "VALUES (?, 'none', ?, ?, ?) ON CONFLICT(test_id) DO UPDATE SET state = 'none', "
                   "reduce_version = excluded.reduce_version, updated_at = excluded.updated_at",
                   (tid, REDUCE_VERSION, now, now))
        stats_["no_metrics"] += 1
        return "read"
    if not cl.exists(f):
        return "missing"
    stats_["pending"] += 1
    db.execute("INSERT INTO hp_test(test_id, state, reduce_version, first_seen, updated_at) "
               "VALUES (?, 'pending', ?, ?, ?) ON CONFLICT(test_id) DO UPDATE SET updated_at = excluded.updated_at",
               (tid, REDUCE_VERSION, now, now))
    first = db.execute("SELECT first_seen FROM hp_test WHERE test_id = ?", (tid,)).fetchone()[0]
    age = time.time() - datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    if age > H.NO_STATUS_GIVE_UP_S:
        db.execute("UPDATE hp_test SET state = 'none' WHERE test_id = ?", (tid,))
    return "pending"


def sync(db, log=print, client: H.Client | None = None, budget_s: float = H.BUDGET_S,
         max_tests: int = H.MAX_TESTS, max_bytes: float | None = None, scale: float = 1.0,
         seed: int = H.SEED_ID) -> dict:
    """The hyperloop-perf flow's walk (hyperloop.walk): the metrics file of every
    passed test, into hp_test. Returns counts."""
    ensure_schema(db)
    cl = client or H.Client()
    t0 = time.monotonic()
    st = {"newest": None, "walked": 0, "read": 0, "no_metrics": 0, "pending": 0, "left": 0, "pruned": 0,
          "est_mb": 0.0, "full": False}
    known = {r[0]: r[1:] for r in db.execute("SELECT test_id, state, reduce_version FROM hp_test")}

    def stale(tid):
        k = known.get(tid)
        return k is None or k[0] == "pending" or k[1] != REDUCE_VERSION

    top, _ = H.walk(db, cl, table="hp_test", stale=stale, read=lambda tid: read(db, cl, tid, st),
                    cost=lambda tid, seen: cost(db, tid), stats=st, budget_s=budget_s, max_tests=max_tests,
                    max_bytes=max_bytes, scale=scale, seed=seed, t0=t0)
    st.update(requests=cl.requests, mb=round(cl.bytes / 1e6, 1), seconds=round(time.monotonic() - t0))
    limit = (f"{max_bytes / 1e6:g} MB" if max_bytes is not None else f"{max_tests} tests")
    log(f"hyperloop-perf: {st['walked']} tests down from {top}, ~{st['est_mb']} MB of index "
        f"({'full: ' if st['full'] else ''}limit {limit}); read {st['read']} metrics file(s), "
        f"{st['no_metrics']} test(s) without, {st['pending']} not finished, {st['left']} left for the next pass, "
        f"{st['pruned']} dropped; {cl.requests} request(s), {st['mb']} MB, {st['seconds']} s")
    return st


# ---------------------------------------------------------------- rows
def _i(v, k=1.0):
    return None if v is None else int(round(v * k))


def rows(r: dict) -> dict:
    """The index rows of one cached test (a dict of hp_test's columns):
    {"test": ... | None, "workflows": [...], "tables": [...], "names": {id: name}}.
    Compact, as hyperloop.rows(): names as ids, whole numbers (kB, ms, tenths of
    a percent); the readers' views (reader.INDEXES) give the readable shape."""
    tid = r["test_id"]
    if r.get("state") != "read" or not r.get("perf"):
        return {"test": None, "workflows": [], "tables": [], "names": {}}
    p = json.loads(r["perf"])
    names: dict[int, str] = {}

    def nid(n):
        i = H.name_id(n)
        names[i] = n
        return i

    tr = p.get("train") or {}
    tp = tr.get("pss") or {}
    test = dict(test_id=tid, processed_bytes=_i(tr.get("processed_bytes")), processed_files=_i(tr.get("processed_files")),
                timeframes=_i(tr.get("timeframes")), ccdb_time=_i(tr.get("ccdb_time")),
                total_ccdb_time=_i(tr.get("total_ccdb_time")), pss_p95_kb=_i(tp.get("p95")), pss_max_kb=_i(tp.get("max")),
                dirty_max_kb=_i(tr.get("dirty_max")), cpu_ms=_i(tr.get("cpu_us"), 1e-3),
                n_workflows=len(p.get("workflows") or {}))
    wfs, tables = [], []
    for wf, m in (p.get("workflows") or {}).items():
        w = nid(wf)
        ps, dt, cp = m.get("pss") or {}, m.get("dirty") or {}, m.get("cpu_pct") or {}
        wfs.append(dict(
            test_id=tid, workflow_id=w, wagon_id=m.get("wagon_id"), n_samples=m.get("n"),
            pss_mean_kb=_i(ps.get("mean")), pss_med_kb=_i(ps.get("med")), pss_p95_kb=_i(ps.get("p95")),
            pss_max_kb=_i(ps.get("max")), pss_slope_kb_min=_i(m.get("pss_slope")),
            dirty_med_kb=_i(dt.get("med")), dirty_p95_kb=_i(dt.get("p95")), dirty_max_kb=_i(dt.get("max")),
            rss_max_kb=_i(m.get("rss_max")), vms_max_kb=_i(m.get("vms_max")), cpu_ms=_i(m.get("cpu_us"), 1e-3),
            cpu_pct_med_x10=_i(cp.get("med"), 10), cpu_pct_p95_x10=_i(cp.get("p95"), 10),
            instructions=_i(m.get("instructions")), ccdb_hit=_i(m.get("ccdb_hit")), ccdb_miss=_i(m.get("ccdb_miss")),
            ccdb_fail=_i(m.get("ccdb_fail")), ccdb_bytes=_i(m.get("ccdb_bytes")), ccdb_queries=_i(m.get("ccdb_queries")),
            out_bytes=_i(m.get("out_bytes")), out_tables=m.get("out_tables") or 0))
        for name, b in m.get("top_tables") or []:
            tables.append(dict(test_id=tid, workflow_id=w, table_id=nid(name), bytes=_i(b)))
    return {"test": test, "workflows": wfs, "tables": tables, "names": names}


def est(r: dict) -> tuple[float, dict[int, str]]:
    """Bytes the test's rows take in hyperloop-perf.db (as hyperloop._vbytes
    counts them), and the names they use."""
    x = rows(r)
    n = 0
    for part in ([x["test"]] if x["test"] else [], x["workflows"], x["tables"]):
        for row in part:
            n += H.ROW_OVERHEAD + sum(H._vbytes(v) for v in row.values())
    return float(n), x["names"]


def cost(db, tid: int) -> float | None:
    """est() of a cached test, stored with it (computed once). None: not cached."""
    r = db.execute("SELECT state, est_bytes, est_version FROM hp_test WHERE test_id = ?", (tid,)).fetchone()
    if r is None:
        return None
    if r[0] != "read":
        return 0.0
    if r[2] != EST_VERSION:
        cols = [d[1] for d in db.execute("PRAGMA table_info(hp_test)")]
        full = dict(zip(cols, db.execute("SELECT * FROM hp_test WHERE test_id = ?", (tid,)).fetchone()))
        n, names = est(full)
        db.execute("UPDATE hp_test SET est_bytes = ?, est_names = ?, est_version = ? WHERE test_id = ?",
                   (n, json.dumps(names), EST_VERSION, tid))
        return n
    return r[1]


def names_of(db, tid: int) -> dict[int, str]:
    cost(db, tid)
    r = db.execute("SELECT est_names FROM hp_test WHERE test_id = ?", (tid,)).fetchone()
    return {int(k): v for k, v in json.loads(r[0] or "{}").items()} if r else {}
