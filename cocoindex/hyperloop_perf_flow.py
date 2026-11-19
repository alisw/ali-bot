"""CocoIndex flow: Hyperloop wagon tests' resource metrics into SQLite
(hyperloop-perf.db), from performanceMetrics_processed.json in their public
folders on alimonitor (hlperf.py, HYPERLOOP_TESTS.md).

A flow of its own, next to hyperloop (the errors): it reads only the metrics
file (and status.json when that is missing, to tell a failed test from a
running one), so the two never fetch the same file, and each is re-extracted
on its own. No embedding: nothing here is text to search.

Each pass:

  1. sync (hlperf.sync, on hyperloop.walk): find the newest test id, walk down
     from it reading the metrics file of every test not cached yet, newest
     first, within HL_BUDGET_S, into the cache (state/alimonitor/perf.db, in
     the state tarball), and stop at HP_MAX_DB_MB of index (hlperf.est, scaled
     by what the real DB measured: scale()). Older tests leave the cache, and
     so the index.
  2. rows: one component per test (grouped by thousand), memoised on a
     fingerprint of its cached row: hp_tests_data, hp_workflows_data,
     hp_tables_data, and the names they use (hp_names).

    FLOW_DB              default hyperloop-perf.db
    HP_CACHE_DB          default $COCOINDEX_DB/alimonitor/perf.db
    HP_MAX_DB_MB         size budget of hyperloop-perf.db, in MB (100)
    HL_*, HP_*           see hyperloop.py, hlperf.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass

import cocoindex as coco
from cocoindex.connectors import sqlite

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import hlperf  # noqa: E402
import hyperloop  # noqa: E402
import metrics  # noqa: E402

METRICS = metrics.install_litellm("hyperloop-perf")

FLOW_DB = os.environ.get("FLOW_DB", "hyperloop-perf.db")
STATE_DIR = pathlib.Path(os.environ.get("COCOINDEX_DB", "."))
CACHE_DB = pathlib.Path(os.environ.get("HP_CACHE_DB", STATE_DIR / "alimonitor" / "perf.db"))
MAX_INFLIGHT_COMPONENTS = int(os.environ.get("COCOINDEX_MAX_INFLIGHT_COMPONENTS", "16"))
MAX_DB_MB = float(os.environ.get("HP_MAX_DB_MB", "100"))
SAMPLE_TESTS = 200       # tests indexed before the DB's own measure replaces DEFAULT_SCALE
DEFAULT_SCALE = 1.2      # real bytes over estimated ones, until measured
# Bump when the rows built from the cache change shape (hlperf.ROWS_VERSION
# covers rows()).
FLOW_VERSION = 1

DB = coco.ContextKey[sqlite.ManagedConnection]("hp_sqlite")


def log(*a):
    print("hyperloop-perf:", *a, file=sys.stderr, flush=True)


def gauge(name: str, value: float) -> None:
    if METRICS is not None:
        METRICS.gauge(f"cocoindex_hp_{name}", value)


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def open_cache() -> sqlite3.Connection:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(CACHE_DB, isolation_level=None, check_same_thread=False)
    hlperf.ensure_schema(c)
    return c


def scale(cache: sqlite3.Connection, flow_db=None) -> tuple[float, dict]:
    """Real bytes of hyperloop-perf.db per estimated byte: its pages in use over
    the estimate of the tests it holds. DEFAULT_SCALE until SAMPLE_TESTS."""
    path = pathlib.Path(flow_db or FLOW_DB)
    used, ids = 0, []
    if path.exists():
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                ps, pc, fl = (c.execute(f"PRAGMA {p}").fetchone()[0]
                              for p in ("page_size", "page_count", "freelist_count"))
                used = (pc - fl) * ps
                ids = [r[0] for r in c.execute("SELECT test_id FROM hp_tests_data")]
            finally:
                c.close()
        except sqlite3.Error:
            used, ids = 0, []
    info = {"db_mb": round(used / 1e6, 1), "indexed": len(ids)}
    if len(ids) < SAMPLE_TESTS:
        return DEFAULT_SCALE, dict(info, scale=DEFAULT_SCALE, measured=False)
    est = sum(hlperf.cost(cache, tid) or 0.0 for tid in ids)
    k = min(max(used / est, 1.0), 20.0) if est else DEFAULT_SCALE
    return k, dict(info, scale=round(k, 2), measured=bool(est))


def sync() -> None:
    db = open_cache()
    try:
        k, m = scale(db)
        log(f"hyperloop-perf.db {m['db_mb']} MB for {m['indexed']} test(s); {k:.2f} real byte(s) per estimated "
            f"one{'' if m['measured'] else ' (not measured yet)'}; stopping at {MAX_DB_MB:g} MB")
        gauge("db_bytes", m["db_mb"] * 1e6)
        gauge("cache_bytes", CACHE_DB.stat().st_size if CACHE_DB.exists() else 0)
        gauge("scale", k)
        st = hlperf.sync(db, max_bytes=MAX_DB_MB * 1e6, scale=k,
                         log=lambda m: log(m.removeprefix("hyperloop-perf: ")))
        for key in ("newest", "walked", "read", "no_metrics", "left", "pruned", "requests", "est_mb"):
            gauge(key, st.get(key) or 0)
    except Exception as e:  # alimonitor unreachable: index what the cache already holds
        log(f"sync failed, indexing the cached tests: {type(e).__name__}: {e}")
        if db.in_transaction:
            db.execute("ROLLBACK")
    db.close()


# ================================================================ rows
# Compact, as the hyperloop flow's: names as ids (hp_names), whole numbers;
# reader.INDEXES adds the views readers query (hp_tests, hp_workflows, hp_tables).
@dataclass
class HPTest:                   # the whole train (__full_train__)
    test_id: int
    processed_bytes: int | None # input processed
    processed_files: int | None
    timeframes: int | None
    ccdb_time: int | None
    total_ccdb_time: int | None
    pss_p95_kb: int | None      # of the whole train
    pss_max_kb: int | None
    dirty_max_kb: int | None
    cpu_ms: int | None
    n_workflows: int


@dataclass
class HPWorkflow:               # one workflow (wagon) of a test: statistics of its ~5 s samples
    test_id: int
    workflow_id: int            # hp_names
    wagon_id: int | None        # NULL: the framework's own (reader, writer)
    n_samples: int | None
    pss_mean_kb: int | None
    pss_med_kb: int | None
    pss_p95_kb: int | None
    pss_max_kb: int | None
    pss_slope_kb_min: int | None   # least squares over the test: growing memory
    dirty_med_kb: int | None    # private dirty: memory this workflow alone wrote
    dirty_p95_kb: int | None
    dirty_max_kb: int | None
    rss_max_kb: int | None
    vms_max_kb: int | None
    cpu_ms: int | None          # total CPU (user + system)
    cpu_pct_med_x10: int | None # CPU %, tenths: 1000 = one core busy
    cpu_pct_p95_x10: int | None
    instructions: int | None    # retired instructions (newer tags)
    ccdb_hit: int | None
    ccdb_miss: int | None
    ccdb_fail: int | None
    ccdb_bytes: int | None
    ccdb_queries: int | None
    out_bytes: int | None       # bytes of the output tables it created
    out_tables: int


@dataclass
class HPTable:                  # one of a workflow's largest output tables (hlperf.TOP_TABLES)
    test_id: int
    workflow_id: int            # hp_names
    table_id: int               # hp_names (the table's name, AOD-less: "HFCAND3PBASE-0")
    bytes: int | None


@dataclass
class HPName:
    name_id: int
    name: str


@dataclass
class Tables:
    tests: object
    workflows: object
    tables: object
    names: object


_cache_ro: sqlite3.Connection | None = None


def _cache() -> sqlite3.Connection:
    global _cache_ro
    if _cache_ro is None:
        _cache_ro = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True, check_same_thread=False)
        _cache_ro.row_factory = sqlite3.Row
    return _cache_ro


@coco.fn(memo=True)
async def process_test(item: tuple[int, str], tables: Tables) -> None:
    r = hlperf.rows(dict(_cache().execute("SELECT * FROM hp_test WHERE test_id = ?", (item[0],)).fetchone()))
    if r["test"]:
        tables.tests.declare_row(row=HPTest(**r["test"]))
    for w in r["workflows"]:
        tables.workflows.declare_row(row=HPWorkflow(**w))
    for t in r["tables"]:
        tables.tables.declare_row(row=HPTable(**t))


@coco.fn(memo=True)
async def process_group(items: tuple[tuple[int, str], ...], tables: Tables) -> None:
    await coco.mount_each(process_test, [(str(it[0]), it) for it in items], tables)


@coco.fn(memo=True)
async def process_names(names: tuple[tuple[int, str], ...], tables: Tables) -> None:
    for i, n in names:
        tables.names.declare_row(row=HPName(i, n))


@coco.lifespan
async def coco_lifespan(builder: coco.EnvironmentBuilder):
    with sqlite.managed_connection(FLOW_DB) as conn:
        builder.provide(DB, conn)
        yield


def plan() -> tuple[dict, dict]:
    """From the cache: per thousand the test items (tests with metrics), and per
    id remainder the names their rows use (stored with each test's estimate)."""
    c = open_cache()
    c.row_factory = sqlite3.Row
    tests: dict[int, list] = defaultdict(list)
    names: dict[int, str] = {}
    for t in c.execute("SELECT test_id, perf FROM hp_test WHERE state = 'read'").fetchall():
        fp = _hash(FLOW_VERSION, hlperf.REDUCE_VERSION, hlperf.ROWS_VERSION, t["perf"])
        tests[t["test_id"] // 1000].append((t["test_id"], fp))
        names.update(hlperf.names_of(c, t["test_id"]))
    c.close()
    name_groups: dict[int, list] = defaultdict(list)
    for i, n in names.items():
        name_groups[i % 64].append((i, n))
    return tests, name_groups


@coco.fn
async def app_main() -> None:
    S = sqlite.TableSchema
    tables = Tables(
        await sqlite.mount_table_target(DB, "hp_tests_data", await S.from_class(HPTest, primary_key=["test_id"])),
        await sqlite.mount_table_target(DB, "hp_workflows_data", await S.from_class(
            HPWorkflow, primary_key=["test_id", "workflow_id"])),
        await sqlite.mount_table_target(DB, "hp_tables_data", await S.from_class(
            HPTable, primary_key=["test_id", "workflow_id", "table_id"])),
        await sqlite.mount_table_target(DB, "hp_names", await S.from_class(HPName, primary_key=["name_id"])))
    await asyncio.to_thread(sync)
    tests, names = await asyncio.to_thread(plan)
    n_tests = sum(map(len, tests.values()))
    log(f"indexing {n_tests} test(s) in {len(tests)} group(s), {sum(map(len, names.values()))} name(s)")
    gauge("tests", n_tests)
    await coco.mount_each(
        coco.component_subpath("names"), process_names,
        [(f"n@{g}", tuple(sorted(v))) for g, v in sorted(names.items())], tables)
    await coco.mount_each(
        coco.component_subpath("tests"), process_group,
        [(f"t@{g}", tuple(sorted(v))) for g, v in sorted(tests.items())], tables)


app = coco.App(
    coco.AppConfig(name="AliceHyperloopPerf", max_inflight_components=MAX_INFLIGHT_COMPONENTS),
    app_main,
)
