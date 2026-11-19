"""CocoIndex flow: Hyperloop wagon tests into SQLite (hyperloop.db), from their
public folders on alimonitor (HYPERLOOP_TESTS.md).

A flow of its own, next to ci-failures: its own cache, state and published DB,
so its size, its pass time and its failures never touch the CI index. Errors
are signed by logscan as CI errors are, so the same error has the same sig_hash
in both: the MCP server joins them (ci_signature, search_ci_failures).

Each pass:

  1. sync (hyperloop.sync): find the newest test id (ids are consecutive: a
     bisection on whether its folder exists), walk down from it reading every
     test not cached yet, newest first, within HL_BUDGET_S, into the cache
     (state/alimonitor/tests.db, in the state tarball: restored with it, like
     ci-failures' logs.db). Tests pushed out of the newest HL_MAX_TESTS leave
     the cache, and so the index: its size is bounded, not its age. The walk
     adds up, test by test, the bytes its rows will take in hyperloop.db
     (hyperloop.est_bytes, scaled by what the real DB measured against that
     estimate: scale()), and stops at HL_MAX_DB_MB.
  2. rows: one component per test (grouped by thousand), memoised on a
     fingerprint of its cached row: hl_tests, hl_test_wagons, hl_metrics,
     hl_errors.
  3. signatures: one component per distinct error signature, the only
     embeddings this flow makes (hl_signatures, "<kind>: <signature>").

    FLOW_DB              default hyperloop.db
    HL_CACHE_DB          default $COCOINDEX_DB/alimonitor/tests.db
    HL_MAX_DB_MB         size budget of hyperloop.db, in MB (100)
    HL_*                 see hyperloop.py (HL_MAX_TESTS: a ceiling in tests)
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
from typing import Annotated, AsyncIterator

import cocoindex as coco
from cocoindex.connectors import sqlite
from cocoindex.ops.litellm import LiteLLMEmbedder
from numpy.typing import NDArray

# logscan.py ships next to this file in the job, and lives in ../ci-mcp in the
# ali-bot checkout (as for ci_failures_flow.py).
_HERE = pathlib.Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "ci-mcp"):
    if (_p / "logscan.py").exists():
        sys.path.insert(0, str(_p))
        break
import logscan  # noqa: E402

sys.path.insert(0, str(_HERE))
import embedtext  # noqa: E402
import embedroute  # noqa: E402
import hyperloop  # noqa: E402
import metrics  # noqa: E402

METRICS = metrics.install_litellm("hyperloop")
embedroute.start_repicker("hyperloop")

FLOW_DB = os.environ.get("FLOW_DB", "hyperloop.db")
STATE_DIR = pathlib.Path(os.environ.get("COCOINDEX_DB", "."))
CACHE_DB = pathlib.Path(os.environ.get("HL_CACHE_DB", STATE_DIR / "alimonitor" / "tests.db"))
EMBED_MODEL = os.environ.get("EMBED_MODEL", "openai/embeddinggemma-300m")
MAX_INFLIGHT_COMPONENTS = int(os.environ.get("COCOINDEX_MAX_INFLIGHT_COMPONENTS", "16"))
EMBED_MAX_INFLIGHT_REQUESTS = int(os.environ.get("EMBED_MAX_INFLIGHT_REQUESTS", "4"))
EMBED_TIMEOUT_S = float(os.environ.get("EMBED_TIMEOUT_S", "300"))
MAX_ERRORS = int(os.environ.get("HL_MAX_ERRORS", "40"))
# The detailed window (tests, wagons, metrics, errors): about a week at ~2.4 kB
# a test and ~1,000 tests a day. The daily summary and the signatures it names
# are bounded by HL_SUMMARY_DAYS instead (hyperloop.SUMMARY_DAYS).
MAX_DB_MB = float(os.environ.get("HL_MAX_DB_MB", "20"))
DETAILED = ("hl_tests_data", "hl_test_wagons_data", "hl_metrics_data", "hl_errors_data")
SAMPLE_TESTS = 200       # tests indexed before the DB's own measure replaces DEFAULT_SCALE
DEFAULT_SCALE = 1.1      # real bytes over estimated ones, until measured: 1.04 on 999 live tests (2026-11-19)
TEXT_CHARS = 1500          # text per error in the cache only (scrubbed, lines clipped)
EMBED_CHARS = 2000
# Bump when the rows built from the cache change shape (hyperloop.ROWS_VERSION
# covers rows()).
FLOW_VERSION = 1

EMBEDDER = coco.ContextKey[LiteLLMEmbedder]("hl_embedder", detect_change=True)
DB = coco.ContextKey[sqlite.ManagedConnection]("hl_sqlite")


def log(*a):
    print("hyperloop:", *a, file=sys.stderr, flush=True)


def gauge(name: str, value: float, **attrs) -> None:
    if METRICS is not None:
        METRICS.gauge(f"cocoindex_hl_{name}", value, **attrs)


# hyperloop.db's tables by part, for the size gauges: the detailed window (the
# size budget), the daily summary and the signatures (HL_SUMMARY_DAYS).
PARTS = {"detailed": DETAILED, "summary": ("hl_error_days", "hl_days"), "signatures": ("hl_signatures",)}


def report_sizes(cache: sqlite3.Connection, flow_db=None) -> dict:
    """Gauges of what the flow keeps: hyperloop.db's bytes per part (as of the
    last pass: dbstat, key indexes included), the cache file, and the summary's
    rows. Returns them."""
    out = {}
    path = pathlib.Path(flow_db or FLOW_DB)
    if path.exists():
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                by = dict(c.execute("SELECT name, sum(pgsize) FROM dbstat GROUP BY name").fetchall())
            finally:
                c.close()
            seen = set()
            for part, tables in PARTS.items():
                names = [n for t in tables for n in (t, f"sqlite_autoindex_{t}_1")]
                out[f"db:{part}"] = sum(by.get(n, 0) for n in names)
                seen.update(names)
            out["db:other"] = sum(v for n, v in by.items() if n not in seen)
        except sqlite3.Error:
            pass
    out["cache"] = CACHE_DB.stat().st_size if CACHE_DB.exists() else 0
    for k, q in (("summary_days", "SELECT count(DISTINCT day) FROM hl_day_test"),
                 ("summary_tests", "SELECT count(*) FROM hl_day_test"),
                 ("summary_pairs", "SELECT count(*) FROM hl_day_sig"),
                 ("summary_signatures", "SELECT count(*) FROM hl_sig")):
        out[k] = cache.execute(q).fetchone()[0]
    for k, v in out.items():
        if k.startswith("db:"):
            gauge("db_bytes", v, part=k[3:])
        elif k == "cache":
            gauge("cache_bytes", v)
        else:
            gauge(k, v)
    return out


def _hash(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def extract(raw: bytes) -> dict:
    return logscan.extract_log(raw, MAX_ERRORS, TEXT_CHARS)


def open_cache() -> sqlite3.Connection:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    # Rollback journal, not WAL: the file is tarred between passes.
    c = sqlite3.connect(CACHE_DB, isolation_level=None, check_same_thread=False)
    hyperloop.ensure_schema(c)
    return c


def scale(cache: sqlite3.Connection, flow_db=None) -> tuple[float, dict]:
    """How many real bytes of hyperloop.db one estimated byte costs: the pages
    in use (not the file: deleted rows leave free pages) over the estimate of
    the tests it holds. DEFAULT_SCALE until SAMPLE_TESTS are indexed."""
    path = pathlib.Path(flow_db or FLOW_DB)
    used, ids = 0, []
    if path.exists():
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                # The detailed tables and their key indexes only (dbstat): the daily
                # summary and the signatures are not in the budget.
                names = [*DETAILED, *(f"sqlite_autoindex_{t}_1" for t in DETAILED)]
                used = c.execute(f"SELECT sum(pgsize) FROM dbstat WHERE name IN ({','.join('?' * len(names))})",
                                 names).fetchone()[0] or 0
                ids = [r[0] for r in c.execute("SELECT test_id FROM hl_tests_data")]
            finally:
                c.close()
        except sqlite3.Error:
            used, ids = 0, []
    info = {"db_mb": round(used / 1e6, 1), "indexed": len(ids)}
    if len(ids) < SAMPLE_TESTS:
        return DEFAULT_SCALE, dict(info, scale=DEFAULT_SCALE, measured=False)
    est = sum(hyperloop.test_cost(cache, tid, None, with_sigs=False) or 0.0 for tid in ids)
    k = min(max(used / est, 1.0), 20.0) if est else DEFAULT_SCALE
    return k, dict(info, scale=round(k, 2), measured=bool(est))


def sync() -> None:
    db = open_cache()
    try:
        k, m = scale(db)
        log(f"hyperloop.db {m['db_mb']} MB for {m['indexed']} test(s); {k:.2f} real byte(s) per estimated one"
            f"{'' if m['measured'] else ' (not measured yet)'}; stopping at {MAX_DB_MB:g} MB")
        gauge("scale", k)
        st = hyperloop.sync(db, extract, logscan.EXTRACT_VERSION, max_bytes=MAX_DB_MB * 1e6, scale=k,
                            log=lambda m: log(m.removeprefix("hyperloop: ")))
        gone = hyperloop.prune_days(db)
        if gone:
            log(f"daily summary: {gone} test(s) older than {hyperloop.SUMMARY_DAYS:g} days dropped")
        z = report_sizes(db)
        log("kept: " + ", ".join(f"{k} {v / 1e6:.1f} MB" if k.startswith("db:") or k == "cache" else f"{k} {v}"
                                 for k, v in z.items()))
        for k in ("newest", "walked", "read", "failed", "left", "pruned", "requests", "est_mb"):
            gauge(k, st.get(k) or 0)
    except Exception as e:  # alimonitor unreachable: index what the cache already holds
        log(f"sync failed, indexing the cached tests: {type(e).__name__}: {e}")
        if db.in_transaction:
            db.execute("ROLLBACK")
    db.close()


# ================================================================ rows
# The published shape is compact (HYPERLOOP_TESTS.md, "Size"): names as ids into
# hl_names, whole numbers, no copies of a test's fields on its other rows, no
# URLs. reader.INDEXES adds the views readers query (hl_tests, hl_test_wagons,
# hl_metrics, hl_errors) with the names, units and URLs back.
@dataclass
class HLTest:                   # one Hyperloop wagon test (hyperloop.rows)
    test_id: int
    end_time: str | None        # UTC
    tag: str | None             # O2Physics::daily-20261119-0000-1
    dataset: str | None         # the first input path
    train_type: str | None
    test_exit: str | None       # done | failed | ...
    merge_exit: str | None
    wall_time: int | None
    cpu_cores: int | None
    n_inputs: int | None
    input_bytes: int | None
    output_bytes: int | None
    derived_bytes: int | None
    n_wagons: int
    max_pss_kb: int | None      # the largest PSS of one DPL device (passed tests)
    cpu_ms: int | None          # CPU of all devices (passed tests)
    n_errors: int | None        # error lines of the logs (failed tests; NULL: not read)
    n_distinct: int | None
    infra_kinds: str | None
    state: str                  # success | failure | unknown


@dataclass
class HLTestWagon:
    test_id: int
    wagon_id: int
    subwagon_id: int            # 0: none given
    workflow_id: int            # hl_names


@dataclass
class HLMetric:                 # per DPL device, from status.json (per workflow: the hyperloop-perf flow)
    test_id: int
    workflow_id: int            # hl_names
    device_id: int              # hl_names
    wagon_id: int | None        # NULL: the framework's own (reader, writer, __full_train__)
    cpu_ms: int | None
    pss_kb: int | None


@dataclass
class HLError:
    test_id: int
    log: str                    # stdout | merge
    sig_hash: str               # logscan's: the same error has the same hash in ci_errors
    rank: int
    device_id: int | None       # hl_names: the DPL device that printed it
    kind: str
    infra: bool
    fatal: bool
    class_hash: str             # the text is in hl_signatures, once per signature
    file: str | None
    count: int
    line: int | None            # 1-based line of <folder>/<log>.log


@dataclass
class HLErrorDay:               # the daily summary (HL_SUMMARY_DAYS): one error on one day
    day: str                    # UTC, the tests' end
    sig_hash: str               # hl_signatures
    tests: int                  # tests that hit it that day
    wagons: int                 # distinct wagons in those tests
    tags: int                   # distinct O2Physics tags
    test_ids: str               # JSON [test_id]: back to each test's folder, to rebuild its details


@dataclass
class HLDay:                    # the tests of one day, the denominators
    day: str
    tests: int
    failed: int


@dataclass
class HLName:                   # a workflow or DPL device name (hyperloop.name_id)
    name_id: int
    name: str


@dataclass
class HLMeta:                   # hl_meta: what the views need besides the rows ("base": the folders' URL)
    key: str
    value: str


@dataclass
class HLSignature:
    sig_hash: str
    class_hash: str
    kind: str
    infra: bool
    sig_exact: str
    sig_class: str
    file: str | None
    embedding: Annotated[NDArray, EMBEDDER]   # of "<kind>: <sig_exact>", as ci_signatures


@dataclass
class Tables:
    tests: object
    wagons: object
    metrics: object
    errors: object
    sigs: object
    names: object
    meta: object
    error_days: object
    days: object


_cache_ro: sqlite3.Connection | None = None


def _cache() -> sqlite3.Connection:
    global _cache_ro
    if _cache_ro is None:
        _cache_ro = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True, check_same_thread=False)
        _cache_ro.row_factory = sqlite3.Row
    return _cache_ro


@coco.fn(memo=True)
async def process_test(item: tuple[int, str], tables: Tables) -> None:
    """One component per test; the fingerprint of its cached row is in the
    arguments, so an unchanged test is a memo hit."""
    r = hyperloop.rows(dict(_cache().execute("SELECT * FROM hl_test WHERE test_id = ?", (item[0],)).fetchone()))
    tables.tests.declare_row(row=HLTest(**r["test"]))
    for w in r["wagons"]:
        tables.wagons.declare_row(row=HLTestWagon(**w))
    for m in r["metrics"]:
        tables.metrics.declare_row(row=HLMetric(**m))
    for e in r["errors"]:
        tables.errors.declare_row(row=HLError(**e))


@coco.fn(memo=True)
async def process_group(items: tuple[tuple[int, str], ...], tables: Tables) -> None:
    """Tests grouped by thousand: only groups with a changed test descend."""
    await coco.mount_each(process_test, [(str(it[0]), it) for it in items], tables)


@coco.fn(memo=True)
async def process_names(names: tuple[tuple[int, str], ...], tables: Tables) -> None:
    for i, n in names:
        tables.names.declare_row(row=HLName(i, n))


@coco.fn(memo=True)
async def process_day(item: tuple[str, tuple[int, int], tuple[tuple[str, int, int, int, str], ...]],
                      tables: Tables) -> None:
    """One day of the summary: memoised on its numbers, so only days that
    changed (today, a backfill) are declared again."""
    day, totals, errs = item
    tables.days.declare_row(row=HLDay(day, *totals))
    for sig_hash, n, wagons, tags, ids in errs:
        tables.error_days.declare_row(row=HLErrorDay(day, sig_hash, n, wagons, tags, ids))


@coco.fn(memo=True)
async def process_meta(base: str, tables: Tables) -> None:
    tables.meta.declare_row(row=HLMeta("base", base))


@coco.fn(memo=True)
async def process_sig(sig: tuple[str, str, str, int, str, str, str], tables: Tables) -> None:
    sig_hash, class_hash, kind, infra, exact, cls, file = sig
    vec = await embedtext.embed_doc(coco.use_context(EMBEDDER), f"{kind}: {exact}"[:EMBED_CHARS])
    tables.sigs.declare_row(row=HLSignature(sig_hash, class_hash, kind, bool(infra), exact, cls, file or None, vec))


@coco.fn(memo=True)
async def process_sig_group(sigs: tuple[tuple, ...], tables: Tables) -> None:
    await coco.mount_each(process_sig, [(s[0], s) for s in sigs], tables)


@coco.lifespan
async def coco_lifespan(builder: coco.EnvironmentBuilder) -> AsyncIterator[None]:
    builder.provide(EMBEDDER, LiteLLMEmbedder(
        EMBED_MODEL, max_inflight_requests=EMBED_MAX_INFLIGHT_REQUESTS, timeout=EMBED_TIMEOUT_S))
    with sqlite.managed_connection(FLOW_DB, load_vec=True) as conn:
        builder.provide(DB, conn)
        yield


def plan() -> tuple[dict, dict, dict]:
    """From the cache: per thousand the test items, per hash prefix the
    signature items (of the failed tests' errors), and per id prefix the names
    their rows use (stored with each test's size estimate: no rows() here)."""
    c = open_cache()
    c.row_factory = sqlite3.Row
    tests: dict[int, list] = defaultdict(list)
    sigs: dict[str, tuple] = {}
    names: dict[int, str] = {}
    for t in c.execute("SELECT test_id, status, devices, config, perf, errors, n_errors, n_distinct "
                       "FROM hl_test WHERE status IS NOT NULL").fetchall():
        fp = _hash(FLOW_VERSION, hyperloop.REDUCE_VERSION, hyperloop.ROWS_VERSION, t["status"], t["devices"],
                   t["config"], t["perf"], t["errors"], t["n_errors"], t["n_distinct"])
        tests[t["test_id"] // 1000].append((t["test_id"], fp))
        names.update(hyperloop.names_of(c, t["test_id"]))
    # The signatures the daily summary names (a superset of the cached tests'):
    # their text once, as first recorded.
    for r in c.execute("SELECT sig_hash, class_hash, kind, infra, sig_exact, sig_class, file FROM hl_sig"):
        sigs[r[0]] = (r[0], r[1], r[2], bool(r[3]), r[4], r[5], r[6] or "")
    c.close()
    groups: dict[str, list] = defaultdict(list)
    for h, s in sigs.items():
        groups[h[:2]].append(s)
    name_groups: dict[int, list] = defaultdict(list)
    for i, n in names.items():
        name_groups[i % 64].append((i, n))
    return tests, groups, name_groups


def plan_days() -> list[tuple[str, tuple[int, int], tuple]]:
    """The daily summary from the cache: per day its totals and, per error, the
    tests, distinct wagons and tags that hit it."""
    c = open_cache()
    totals = {d: (n, f or 0) for d, n, f in c.execute(
        "SELECT day, count(*), sum(state = 'failure') FROM hl_day_test GROUP BY day")}
    acc: dict[tuple[str, str], list] = {}
    for tid, day, sig, tag, wagons in c.execute(
            "SELECT test_id, d.day, s.sig_hash, d.tag, d.wagons FROM hl_day_sig s JOIN hl_day_test d USING (test_id)"):
        a = acc.setdefault((day, sig), [0, set(), set(), []])
        a[0] += 1
        a[1].update(json.loads(wagons or "[]"))
        a[2].add(tag)
        a[3].append(tid)
    c.close()
    per_day: dict[str, list] = defaultdict(list)
    for (day, sig), (n, w, t, ids) in acc.items():
        per_day[day].append((sig, n, len(w), len(t - {None}), json.dumps(sorted(ids))))
    return [(d, totals[d], tuple(sorted(per_day.get(d, [])))) for d in sorted(totals)]


@coco.fn
async def app_main() -> None:
    S = sqlite.TableSchema
    tables = Tables(
        await sqlite.mount_table_target(DB, "hl_tests_data", await S.from_class(HLTest, primary_key=["test_id"])),
        await sqlite.mount_table_target(DB, "hl_test_wagons_data", await S.from_class(
            HLTestWagon, primary_key=["test_id", "wagon_id", "subwagon_id", "workflow_id"])),
        await sqlite.mount_table_target(DB, "hl_metrics_data", await S.from_class(
            HLMetric, primary_key=["test_id", "workflow_id", "device_id"])),
        await sqlite.mount_table_target(DB, "hl_errors_data", await S.from_class(
            HLError, primary_key=["test_id", "log", "sig_hash"])),
        await sqlite.mount_table_target(DB, "hl_signatures", await S.from_class(
            HLSignature, primary_key=["sig_hash"])),
        await sqlite.mount_table_target(DB, "hl_names", await S.from_class(HLName, primary_key=["name_id"])),
        await sqlite.mount_table_target(DB, "hl_meta", await S.from_class(HLMeta, primary_key=["key"])),
        await sqlite.mount_table_target(DB, "hl_error_days", await S.from_class(
            HLErrorDay, primary_key=["day", "sig_hash"])),
        await sqlite.mount_table_target(DB, "hl_days", await S.from_class(HLDay, primary_key=["day"])))
    await asyncio.to_thread(sync)
    tests, groups, names = await asyncio.to_thread(plan)
    n_tests, n_sigs = sum(map(len, tests.values())), sum(map(len, groups.values()))
    log(f"indexing {n_tests} test(s) in {len(tests)} group(s), {n_sigs} distinct error signature(s), "
        f"{sum(map(len, names.values()))} name(s)")
    gauge("tests", n_tests)
    gauge("signatures", n_sigs)
    await coco.mount_each(coco.component_subpath("meta"), process_meta, [("base", hyperloop.BASE)], tables)
    days = await asyncio.to_thread(plan_days)
    log(f"daily summary: {len(days)} day(s), {sum(len(e) for _, _, e in days)} error-day row(s)")
    await coco.mount_each(coco.component_subpath("days"), process_day,
                          [(d, (d, totals, errs)) for d, totals, errs in days], tables)
    await coco.mount_each(
        coco.component_subpath("names"), process_names,
        [(f"n@{g}", tuple(sorted(v))) for g, v in sorted(names.items())], tables)
    await coco.mount_each(
        coco.component_subpath("tests"), process_group,
        [(f"t@{g}", tuple(sorted(v))) for g, v in sorted(tests.items())], tables)
    await coco.mount_each(
        coco.component_subpath("sigs"), process_sig_group,
        [(g, tuple(sorted(v))) for g, v in sorted(groups.items())], tables)

app = coco.App(
    coco.AppConfig(name="AliceHyperloopTests", max_inflight_components=MAX_INFLIGHT_COMPONENTS),
    app_main,
)
