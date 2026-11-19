"""Hyperloop wagon tests, read from their public folders on alimonitor, for the
hyperloop flow (hyperloop_flow.py, HYPERLOOP_TESTS.md).

Every test has a folder

    <HL_BASE><id // 10000, 4 digits>/<id, 8 digits>/

with status.json (exit codes, sizes, per-device CPU seconds and PSS),
full_config.json (O2Physics tag, input data, the wagons as workflows), the
logs, and when it passed performanceMetrics_processed.json (read by the
hyperloop-perf flow: hlperf.py). sync() keeps, in the hyperloop flow's cache
(state/alimonitor/tests.db, table hl_test), a reduced copy of each of the
newest tests: the status and the config for every test, the errors of the logs
(logscan, as for CI) for failed ones only. Nothing large is read: no ROOT
file, no profile. walk() and the HTTP client are shared with hlperf.py.

alimonitor is a shared service: one request at a time, at most HL_RATE_PER_S
a second, gzip, and a time budget per pass (HL_BUDGET_S); the backfill
spreads over passes, newest tests first.

Test ids are consecutive and a test runs once: its folder never changes once
status.json is there. So no listing is read: the newest id is found from status
codes alone (newest_id: double, then bisect, on whether <id>/ exists), and
sync() walks down from it, reading the tests it has not cached yet (a test
whose folder exists without status.json yet is asked again each pass).

The index is bounded by size, not by age: the walk, newest first, adds up the
bytes each test's index rows will take, and stops when they fill the budget (or
at HL_MAX_TESTS tests). So it reaches as far back as the budget allows, and each
new test pushes the oldest out of the cache, and so out of the index.

    HL_BASE          default http://alimonitor.cern.ch/train-workdir/tests/ (see BASE)
    HL_MAX_TESTS     at most the newest N test folders are indexed, older ones dropped
                     (100000; the flow also stops at a size: HL_MAX_DB_MB)
    HL_BUDGET_S      seconds of fetching per pass (900)
    HL_RATE_PER_S    requests per second at most (4)
    HL_SEED_ID       a test known to exist, where an empty cache starts its search (809001)
    HL_CAFILE        CA bundle, for an https:// base (default /alloc/cern-ca-bundle.pem
                     if present: alimonitor's certificate is from the CERN Grid CA)
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Plain HTTP: alimonitor's HTTPS refuses the TLS handshake of Python's OpenSSL
# (3.6: "alert handshake failure", whatever the version, cipher or groups offered,
# 2026-11-19), while curl and the OpenSSL 4 CLI get through. The folders are
# public and read only; set an https:// base once that is fixed.
BASE = os.environ.get("HL_BASE", "http://alimonitor.cern.ch/train-workdir/tests/").rstrip("/") + "/"
MAX_TESTS = int(os.environ.get("HL_MAX_TESTS", "100000"))
BUDGET_S = float(os.environ.get("HL_BUDGET_S", "900"))
RATE = float(os.environ.get("HL_RATE_PER_S", "4"))
CAFILE = os.environ.get("HL_CAFILE", "/alloc/cern-ca-bundle.pem")
SEED_ID = int(os.environ.get("HL_SEED_ID", "809001"))
# Days the daily error summary (and the signatures it names) is kept: the
# detailed tests only for the size budget (about a week).
SUMMARY_DAYS = float(os.environ.get("HL_SUMMARY_DAYS", "180"))
# Going down, this many missing folders in a row: the bottom of what alimonitor keeps.
MISSING_STOP = 20
# How long that bottom is trusted before it is probed again: a hole in the ids
# (a cleanup, say) mistaken for it must not stop the backfill for good.
FLOOR_TTL_S = 7 * 86400
TIMEOUT_S = 60
# A test whose folder shows no status.json this long after it last changed
# is given up on (it never finished); younger ones are asked again next pass.
NO_STATUS_GIVE_UP_S = 86400
# Bump when the reduction below changes: every test is read again.
REDUCE_VERSION = 2
# Bump when rows() changes: the indexed rows are rebuilt, nothing is fetched.
ROWS_VERSION = 5


def folder(test_id: int) -> str:
    return f"{test_id // 10000:04d}/{test_id:08d}/"


def folder_url(test_id: int) -> str:
    return BASE + folder(test_id)


# ---------------------------------------------------------------- listings
# Tomcat's directory listing: one <tr> per entry, the name in an <a><tt>, then
# the size and the modification time ("Thu, 19 Nov 2026 12:08:49 GMT").
# ---------------------------------------------------------------- HTTP
class Client:
    """GETs from alimonitor, one at a time, at most RATE a second, gzip."""

    def __init__(self, base: str = BASE, rate: float = RATE, cafile: str | None = CAFILE):
        self.base, self.min_gap = base, (1.0 / rate if rate > 0 else 0.0)
        ctx = ssl.create_default_context()
        if cafile and os.path.exists(cafile):
            ctx.load_verify_locations(cafile=cafile)
        self.ctx = ctx
        self._last = 0.0
        self.requests = self.bytes = 0

    def get(self, path: str) -> bytes | None:
        """The body, or None for a 404."""
        wait = self._last + self.min_gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        req = urllib.request.Request(self.base + path, headers={"Accept-Encoding": "gzip",
                                                                 "User-Agent": "alice-cocoindex/1"})
        self.requests += 1
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=self.ctx) as r:
                body = r.read()
                self.bytes += len(body)
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def exists(self, path: str) -> bool:
        """Whether path is there: a HEAD, the status code only."""
        wait = self._last + self.min_gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        req = urllib.request.Request(self.base + path, method="HEAD", headers={"User-Agent": "alice-cocoindex/1"})
        self.requests += 1
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=self.ctx):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise

    def json(self, path: str):
        b = self.get(path)
        return None if b is None else json.loads(b)


# ---------------------------------------------------------------- reductions
def reduce_status(s: dict) -> tuple[dict, list[dict]]:
    """status.json -> (its scalar fields, one row per DPL device: workflow,
    device, CPU seconds, PSS bytes). Failed tests have no device metrics."""
    scalars = {k: v for k, v in s.items() if not isinstance(v, (dict, list))}
    devices = []
    for wf, devs in (s.get("device_metrics") or {}).items():
        for dev, m in (devs or {}).items():
            devices.append({"workflow": wf, "device": dev, "cpu_s": m.get("CPU seconds"), "pss_bytes": m.get("PSS")})
    return scalars, devices


def reduce_config(c: dict) -> dict:
    """full_config.json -> what identifies the test: the package tag, the input
    data, the train type and cores, and the wagons (workflows with their ids).
    The configurables themselves are not kept."""
    wagons = []
    for w in c.get("workflows") or []:
        wagons.append({"wagon_id": _int(w.get("wagon_id")), "subwagon_id": _int(w.get("subwagon_id")),
                       "workflow_name": w.get("workflow_name")})
    inp = c.get("inputdata") or []
    return {"package_tag": c.get("package_tag"), "dataset": inp[0] if inp else None, "n_datasets": len(inp),
            "type": c.get("type"), "cpu_cores": c.get("cpu_cores"), "derived_data": c.get("derived_data"),
            "train_id": c.get("train_id"), "wagons": wagons}


# The DPL device an error names: its line's prefix '[pid:device]: ...', a crash
# signature 'crash in <device> (<exe>): ...', or the driver's own reports
# ('Device X (pid) had ...', '- Device X: pid ...', 'PID n (X) did not exit ...').
_DEVICE_RES = (re.compile(r"^\[\d+:([^\]\s]+)\]:"),
               re.compile(r"^crash in ([^\s(]+) "),
               re.compile(r"\bDevice ([A-Za-z0-9_.-]+)(?: \(\d+\)|: pid )"),
               re.compile(r"\bPID \d+ \(([A-Za-z0-9_.-]+)\) did not exit"))


def device_of(text: str, sig_exact: str | None = None) -> str | None:
    """The DPL device of an error, from its signature (crashes) or its text."""
    for t in (sig_exact or "", text or ""):
        for rx in _DEVICE_RES:
            m = rx.search(t) if rx.pattern[0] != "^" else rx.match(t)
            if m:
                return m.group(1)
    return None


def failed(status: dict) -> bool:
    return status.get("test_exitcode") != "done" or status.get("merge_exitcode") not in ("done", None)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _iso_ms(ms) -> str | None:
    try:
        return datetime.fromtimestamp(float(ms) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------- the cache
SCHEMA = """
  CREATE TABLE IF NOT EXISTS hl_test(        -- one per Hyperloop test, reduced (hyperloop.py)
    test_id INTEGER PRIMARY KEY,
    folder_mtime TEXT,      -- unused (folders were listed before 2026-11-19)
    status TEXT,            -- JSON: status.json's scalar fields; NULL: none yet
    devices TEXT,           -- JSON: [{workflow, device, cpu_s, pss_bytes}]
    config TEXT,            -- JSON: reduce_config()
    perf TEXT,              -- unused (performanceMetrics: the hyperloop-perf flow, since 2026-11-19)
    extracted INTEGER DEFAULT 0, -- logscan.EXTRACT_VERSION the logs were read with (failed tests)
    errors TEXT,            -- JSON list: logscan errors, each with "log" (stdout|merge) and "device"
    n_errors INTEGER, n_distinct INTEGER, log_bytes INTEGER,
    reduce_version INTEGER, gave_up INTEGER DEFAULT 0, first_seen TEXT, updated_at TEXT,
    est_rows REAL,          -- est_parts(): bytes of its rows in hyperloop.db, signatures aside
    est_sigs TEXT,          -- JSON {sig_hash: bytes}: its signatures, counted once per walk
    est_names TEXT,         -- JSON {name_id: name}: the workflow and device names its rows use
    est_version INTEGER);   -- EST_VERSION they were computed with; NULL: to compute
  CREATE TABLE IF NOT EXISTS hl_state(key TEXT PRIMARY KEY, value);   -- "floor": see walk()
  -- The daily summary (HL_SUMMARY_DAYS): recorded when a test is read, kept after
  -- the test leaves hl_test (the detailed window), dropped by prune_days().
  CREATE TABLE IF NOT EXISTS hl_day_test(test_id INTEGER PRIMARY KEY, day TEXT, tag TEXT, state TEXT,
                                         wagons TEXT);                 -- JSON [wagon_id]
  CREATE INDEX IF NOT EXISTS hl_day_test_day ON hl_day_test(day);
  CREATE TABLE IF NOT EXISTS hl_day_sig(test_id INTEGER, sig_hash TEXT, PRIMARY KEY(test_id, sig_hash));
  CREATE TABLE IF NOT EXISTS hl_sig(sig_hash TEXT PRIMARY KEY, class_hash TEXT, kind TEXT, infra INTEGER,
                                    sig_exact TEXT, sig_class TEXT, file TEXT);  -- what hl_signatures embeds
"""


def ensure_schema(db) -> None:
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(hl_test)")}
    for col, typ in (("est_rows", "REAL"), ("est_sigs", "TEXT"), ("est_names", "TEXT"), ("est_version", "INTEGER")):
        if col not in cols:     # a cache from before the size estimates
            db.execute(f"ALTER TABLE hl_test ADD COLUMN {col} {typ}")


def prune(db, max_tests: int = MAX_TESTS) -> int:
    """Keep the newest max_tests tests of the cache. Returns how many went."""
    nth = db.execute("SELECT test_id FROM hl_test ORDER BY test_id DESC LIMIT 1 OFFSET ?",
                     (max(max_tests, 1) - 1,)).fetchone()
    return db.execute("DELETE FROM hl_test WHERE test_id < ?", (nth[0],)).rowcount if nth else 0


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def newest_id(cl: Client, start: int) -> int | None:
    """The newest test id. Ids are consecutive, so a folder exists for every id
    up to the newest and for none above: from `start` (an id that exists),
    double the step until a folder is missing, then bisect. ~2 log2(new tests)
    HEAD requests. If `start` is missing (an old seed whose folder alimonitor
    dropped), step up by thousands to the first that exists. None: none found."""
    if not cl.exists(folder(start)):
        for k in range(1, 1001):
            if cl.exists(folder(start + 1000 * k)):
                start += 1000 * k
                break
        else:
            return None
    lo, step = start, 1
    while cl.exists(folder(lo + step)):
        lo, step = lo + step, step * 2
    hi = lo + step
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if cl.exists(folder(mid)):
            lo = mid
        else:
            hi = mid
    return lo


def walk(db, cl: Client, *, table: str, stale, read, cost, stats: dict, budget_s: float, max_tests: int,
         max_bytes: float | None, scale: float, seed: int, t0: float) -> tuple[int | None, float]:
    """The walk both Hyperloop flows share (hyperloop, hyperloop-perf), over the
    test cache `table` (a test_id column) of `db`: find the newest test id, then
    go down from it, newest first, calling read(tid) -> "read" | "pending" |
    "missing" on each test for which stale(tid) (until `budget_s` since t0 is
    spent), and stop when the walk holds max_tests tests or, with max_bytes,
    when cost(tid, seen) x scale summed as the walk goes would fill max_bytes
    (plus SLACK for the tests already cached). What lies below leaves `table`.
    Returns (the newest id, the estimated bytes walked)."""
    db.execute("CREATE TABLE IF NOT EXISTS hl_state(key TEXT PRIMARY KEY, value)")
    lo_hi = db.execute(f"SELECT min(test_id), max(test_id) FROM {table}").fetchone()
    top = newest_id(cl, max(lo_hi[1] or 0, seed))
    stats["newest"] = top
    lowest = lo_hi[0] if lo_hi[0] is not None else (top or 0)
    # The highest id found missing below the tests kept (alimonitor's history
    # ends there): not probed again every pass, for FLOOR_TTL_S.
    f = db.execute("SELECT value FROM hl_state WHERE key = 'floor'").fetchone()
    f = json.loads(f[0]) if f else {}
    floor = f.get("id", 0) if time.time() - f.get("at", 0) < FLOOR_TTL_S else 0
    total, n_costed, seen, misses = 0.0, 0, set(), 0
    end = None              # the oldest test id of the window, once a limit is reached
    tid = top or 0
    while tid > floor:
        filled = max_bytes is not None and total >= max_bytes
        out_of_time = time.monotonic() - t0 > budget_s
        if (filled or out_of_time) and tid < lowest:
            break           # nothing cached below, and nothing more to read
        c = None
        if not filled and stale(tid):
            if out_of_time:
                stats["left"] += 1      # next pass; meanwhile it counts as an average test
                c = total / n_costed if n_costed else 0.0
            elif read(tid) == "missing":
                misses += 1
                if misses >= MISSING_STOP:   # the bottom of what alimonitor keeps
                    db.execute("INSERT OR REPLACE INTO hl_state VALUES ('floor', ?)",
                               (json.dumps({"id": tid + MISSING_STOP - 1, "at": time.time()}),))
                    break
                tid -= 1
                continue
        misses = 0
        stats["walked"] += 1
        if max_bytes is not None:
            if c is None:
                b = cost(tid, seen)
                c = (b or 0.0) * scale
                n_costed += b is not None and b > 0
            total += c
        # Filled: nothing more is read, but the tests already cached stay up to
        # SLACK beyond the budget, so a small change of `scale` between passes
        # does not drop the oldest one pass and fetch them again the next.
        if stats["walked"] >= max_tests or (max_bytes is not None and total >= max_bytes * (1 + SLACK)):
            end = tid
            break
        tid -= 1
    # The window ends at `end`: what is older leaves the cache. Only when a limit
    # was reached (a failed search must not empty the cache).
    if end is not None:
        stats["pruned"] = db.execute(f"DELETE FROM {table} WHERE test_id < ?", (end,)).rowcount
    stats["full"] = end is not None or (max_bytes is not None and total >= max_bytes)
    stats["est_mb"] = round(total / 1e6, 1)
    return top, total


def sync(db, extract, extract_version: int, log=print, client: Client | None = None,
         budget_s: float = BUDGET_S, max_tests: int = MAX_TESTS, max_bytes: float | None = None,
         scale: float = 1.0, seed: int = SEED_ID) -> dict:
    """The hyperloop flow's walk (walk()): status.json and full_config.json of
    every test, the logs of failed ones, into hl_test. Returns counts.
    `extract(raw) -> dict` is logscan's extraction, as for CI logs."""
    ensure_schema(db)
    cl = client or Client()
    t0 = time.monotonic()
    stats = {"newest": None, "walked": 0, "read": 0, "failed": 0, "no_status": 0, "left": 0, "pruned": 0,
             "est_mb": 0.0, "full": False}
    known = {r[0]: r[1:] for r in db.execute(
        "SELECT test_id, status IS NOT NULL OR gave_up, reduce_version, "
        "errors IS NOT NULL AND extracted < ? FROM hl_test", (extract_version,))}

    def stale(tid):   # not cached, still without status, reduced by an older version, or old logscan
        k = known.get(tid)
        return k is None or not k[0] or k[1] != REDUCE_VERSION or bool(k[2])

    top, _ = walk(db, cl, table="hl_test", stale=stale,
                  read=lambda tid: _read_test(db, cl, tid, extract, extract_version, stats),
                  # the detailed rows only: signatures live as long as the daily summary names them
                  cost=lambda tid, seen: test_cost(db, tid, seen, with_sigs=False), stats=stats, budget_s=budget_s,
                  max_tests=max_tests, max_bytes=max_bytes, scale=scale, seed=seed, t0=t0)
    oldest = db.execute("SELECT status FROM hl_test WHERE status IS NOT NULL ORDER BY test_id LIMIT 1").fetchone()
    stats["oldest"] = (_iso_ms(json.loads(oldest[0]).get("test_end_time")) or "")[:10] or None if oldest else None
    stats.update(requests=cl.requests, mb=round(cl.bytes / 1e6, 1), seconds=round(time.monotonic() - t0))
    limit = (f"{max_bytes / 1e6:g} MB" if max_bytes is not None else f"{max_tests} tests")
    log(f"hyperloop: {stats['walked']} tests down from {top}, back to {stats['oldest']}, "
        f"~{stats['est_mb']} MB of index ({'full: ' if stats['full'] else ''}limit {limit}); "
        f"read {stats['read']} ({stats['failed']} failed), {stats['no_status']} without status yet, "
        f"{stats['left']} left for the next pass, {stats['pruned']} dropped; "
        f"{cl.requests} request(s), {stats['mb']} MB, {stats['seconds']} s")
    return stats


def _as_dict(db, row) -> dict:
    return dict(row) if isinstance(row, sqlite3.Row) else \
        dict(zip([d[1] for d in db.execute("PRAGMA table_info(hl_test)")], row))


SLACK = 0.05             # cached tests kept beyond the size budget (see sync)

# Bytes of one embedded signature: the vector (768 float32) and its text.
SIG_BYTES = 768 * 4
ROW_OVERHEAD = 40        # SQLite record header, rowid and primary-key index entry, roughly


# Bump when est_parts() changes; ROWS_VERSION is in it, as rows() is what it measures.
EST_VERSION = ROWS_VERSION * 1000 + 2


def est_parts(r: dict) -> tuple[float, dict[str, int]]:
    """The bytes one cached test adds to hyperloop.db, in two parts: its rows as
    rows() builds them, and {sig_hash: bytes} of the signatures it embeds (a
    signature shared by tests is counted once, at the newest: see est_bytes).
    A raw estimate; the flow scales it by what the real DB measured."""
    return _est(r)[:2]


def _est(r: dict) -> tuple[float, dict[str, int], dict[int, str]]:
    if not r.get("status"):
        return 0.0, {}, {}
    x = rows(r)
    n = 0
    for part in (x["wagons"], x["metrics"], x["errors"], [x["test"]]):
        for row in part:
            n += ROW_OVERHEAD + sum(_vbytes(v) for v in row.values())
    sigs = {e["sig_hash"]: ROW_OVERHEAD + SIG_BYTES + len(e.get("sig_exact") or "") + len(e.get("sig_class") or "")
            for e in x["errors"]}
    return float(n), sigs, x["names"]


def _vbytes(v) -> int:
    """Bytes of one value in a SQLite record: integers in 0-8 bytes by size, a
    float in 8, text by its length (sig_exact/sig_class included: the flow's DB
    holds them; publishing drops them)."""
    if v is None or isinstance(v, bool):
        return 1
    if isinstance(v, int):
        a = abs(v)
        return 1 + next((n for n, lim in ((1, 1 << 7), (2, 1 << 15), (3, 1 << 23), (4, 1 << 31), (6, 1 << 47))
                         if a < lim), 8)
    if isinstance(v, float):
        return 9
    return 1 + len(str(v).encode())


def _add_sigs(n: float, parts: dict, seen: set | None) -> float:
    for h, b in parts.items():
        if seen is None or h not in seen:
            n += b
            if seen is not None:
                seen.add(h)
    return n


def est_bytes(r: dict, sigs: set | None = None) -> float:
    """est_parts() as one number: the rows, and the signatures `sigs` (updated)
    has not seen yet (all of them, without `sigs`)."""
    n, parts = est_parts(r)
    return _add_sigs(n, parts, sigs) if r.get("status") else 0.0


def names_of(db, tid: int) -> dict[int, str]:
    """{name_id: name} of a cached test's rows, from what test_cost() stored."""
    r = db.execute("SELECT est_names, est_version FROM hl_test WHERE test_id = ?", (tid,)).fetchone()
    if r and r[1] != EST_VERSION:
        test_cost(db, tid, set())
        r = db.execute("SELECT est_names, est_version FROM hl_test WHERE test_id = ?", (tid,)).fetchone()
    return {int(k): v for k, v in json.loads(r[0] or "{}").items()} if r else {}


def test_cost(db, tid: int, sigs: set | None, with_sigs: bool = True) -> float | None:
    """est_bytes() of a cached test, from the estimate stored with it: computed
    (rows() on its cached JSON) only when missing or from an older EST_VERSION,
    so a pass over a full window costs a lookup per test. None: not cached."""
    r = db.execute("SELECT status IS NOT NULL, est_rows, est_sigs, est_version FROM hl_test WHERE test_id = ?",
                   (tid,)).fetchone()
    if r is None:
        return None
    if not r[0]:
        return 0.0
    if r[3] != EST_VERSION:
        full = db.execute("SELECT * FROM hl_test WHERE test_id = ?", (tid,)).fetchone()
        n, parts, names = _est(_as_dict(db, full))
        db.execute("UPDATE hl_test SET est_rows = ?, est_sigs = ?, est_names = ?, est_version = ? WHERE test_id = ?",
                   (n, json.dumps(parts), json.dumps(names), EST_VERSION, tid))
    else:
        n, parts = r[1], json.loads(r[2] or "{}")
    return _add_sigs(n, parts, sigs) if with_sigs else n


def _read_test(db, cl: Client, tid: int, extract, extract_version: int, stats: dict) -> str:
    """Read one test into the cache: "read", "pending" (its folder exists, its
    status.json not yet: kept, asked again next pass, given up on a day after it
    was first seen) or "missing" (no folder)."""
    f = folder(tid)
    st = cl.json(f + "status.json")
    if st is None:
        if not cl.exists(f):
            return "missing"
        stats["no_status"] += 1
        db.execute("INSERT INTO hl_test(test_id, reduce_version, gave_up, first_seen, updated_at) VALUES (?,?,0,?,?) "
                   "ON CONFLICT(test_id) DO UPDATE SET updated_at = excluded.updated_at", (tid, REDUCE_VERSION,
                                                                                         _now(), _now()))
        first = db.execute("SELECT first_seen FROM hl_test WHERE test_id = ?", (tid,)).fetchone()[0]
        age = time.time() - datetime.strptime(first, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        if age > NO_STATUS_GIVE_UP_S:   # never finished: remember it, do not ask again
            db.execute("UPDATE hl_test SET gave_up = 1 WHERE test_id = ?", (tid,))
        return "pending"
    scalars, devices = reduce_status(st)
    cfg = cl.json(f + "full_config.json")
    config = reduce_config(cfg) if cfg else None
    perf = errors = None
    n_err = n_dist = log_bytes = None
    extracted = 0
    if failed(scalars):
        stats["failed"] += 1
        errors, n_err, n_dist, log_bytes = [], 0, 0, 0
        for name in ("stdout.log", "merge.log"):
            raw = cl.get(f + name)
            if not raw:
                continue
            log_bytes += len(raw)
            x = extract(raw)
            n_err += x.get("n_errors") or 0
            n_dist += x.get("n_distinct") or 0
            for e in x.get("errors") or []:
                e = dict(e, log=name.split(".")[0], device=device_of(e.get("text") or ""))
                errors.append(e)
        extracted = extract_version
    db.execute(
        """INSERT INTO hl_test(test_id, status, devices, config, perf, extracted, errors,
                               n_errors, n_distinct, log_bytes, reduce_version, gave_up, first_seen, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)
           ON CONFLICT(test_id) DO UPDATE SET status=excluded.status,
             devices=excluded.devices, config=excluded.config, perf=excluded.perf, extracted=excluded.extracted,
             errors=excluded.errors, n_errors=excluded.n_errors, n_distinct=excluded.n_distinct,
             log_bytes=excluded.log_bytes, reduce_version=excluded.reduce_version, gave_up=0,
             updated_at=excluded.updated_at, est_version=NULL""",
        (tid, json.dumps(scalars), json.dumps(devices), json.dumps(config) if config else None,
         json.dumps(perf) if perf else None, extracted, json.dumps(errors) if errors is not None else None,
         n_err, n_dist, log_bytes, REDUCE_VERSION, _now(), _now()))
    test_cost(db, tid, set())   # its size estimate, stored with it
    _record_day(db, tid, scalars, config, errors)
    stats["read"] += 1
    return "read"


def _record_day(db, tid: int, scalars: dict, config: dict | None, errors: list | None) -> None:
    """The test's part of the daily summary: its day, tag, state and wagons, and
    the signatures of its errors (their text once, in hl_sig, for embedding)."""
    day = (_iso_ms(scalars.get("test_end_time")) or _now())[:10]
    tag = ((config or {}).get("package_tag") or "").split("@", 1)[-1] or None
    wagons = sorted({w["wagon_id"] for w in (config or {}).get("wagons") or [] if w.get("wagon_id") is not None})
    db.execute("INSERT OR REPLACE INTO hl_day_test VALUES (?,?,?,?,?)",
               (tid, day, tag, "failure" if failed(scalars) else "success", json.dumps(wagons)))
    db.execute("DELETE FROM hl_day_sig WHERE test_id = ?", (tid,))
    for e in errors or []:
        db.execute("INSERT OR IGNORE INTO hl_day_sig VALUES (?,?)", (tid, e["sig_hash"]))
        db.execute("INSERT OR IGNORE INTO hl_sig VALUES (?,?,?,?,?,?,?)",
                   (e["sig_hash"], e["class_hash"], e["kind"], int(bool(e["infra"])), e["sig_exact"],
                    e["sig_class"], e.get("file")))


def prune_days(db, keep_days: float = SUMMARY_DAYS) -> int:
    """Drop the daily summary older than keep_days, and the signatures no day
    names any more. Returns the tests dropped."""
    cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - keep_days * 86400))
    n = db.execute("DELETE FROM hl_day_test WHERE day < ?", (cutoff,)).rowcount
    db.execute("DELETE FROM hl_day_sig WHERE test_id NOT IN (SELECT test_id FROM hl_day_test)")
    db.execute("DELETE FROM hl_sig WHERE sig_hash NOT IN (SELECT sig_hash FROM hl_day_sig)")
    return n


# ---------------------------------------------------------------- rows
def name_id(name: str | None) -> int | None:
    """A workflow or device name's id in hl_names: 47 bits of its SHA-256, so
    every test's rows agree on it without a shared counter (stored in 6 bytes;
    collisions among a few thousand names: ~1e-7). "" (a per-workflow row's
    device) is 0."""
    if name is None:
        return None
    if name == "":
        return 0
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:6], "big") >> 1


def rows(r: dict) -> dict:
    """The index rows of one cached test (a dict of hl_test's columns):
    {"test": ..., "wagons": [...], "metrics": [...], "errors": [...], "names": {id: name}}.
    Compact (HYPERLOOP_TESTS.md, "Size"): names as ids (hl_names), whole numbers
    (kB, ms, counts), no copy of the test's end time, tag or state on its other
    rows, no URL (both from the test id): the readers' views (reader.INDEXES)
    give the readable shape back."""
    tid = r["test_id"]
    st = json.loads(r["status"]) if r["status"] else {}
    cfg = json.loads(r["config"]) if r["config"] else {}
    devs = json.loads(r["devices"]) if r["devices"] else []
    errs = json.loads(r["errors"]) if r["errors"] else []
    tag = (cfg.get("package_tag") or "").split("@", 1)[-1] or None
    end = _iso_ms(st.get("test_end_time"))
    wagons = cfg.get("wagons") or []
    wf_wagon: dict[str, int | None] = {}
    for w in wagons:   # a workflow run by two different wagons cannot be attributed to either
        name, wid = w.get("workflow_name"), w.get("wagon_id")
        wf_wagon[name] = wid if wf_wagon.get(name, wid) == wid else None
    test = dict(
        test_id=tid, end_time=end, tag=tag, dataset=cfg.get("dataset"), train_type=cfg.get("type"),
        test_exit=st.get("test_exitcode"), merge_exit=st.get("merge_exitcode"), wall_time=_int(st.get("test_wall_time")),
        cpu_cores=_int(st.get("cpu_cores")), n_inputs=_int(st.get("test_number_of_input_files")),
        input_bytes=_int(_num(st.get("test_input_size"))), output_bytes=_int(_num(st.get("test_output_size"))),
        derived_bytes=_int(_num(st.get("test_derived_output_size"))), n_wagons=len({w.get("wagon_id") for w in wagons}),
        max_pss_kb=_kb(max((d["pss_bytes"] or 0 for d in devs), default=None) or None),
        cpu_ms=_ms(sum(d["cpu_s"] or 0 for d in devs)) if devs else None,
        n_errors=r["n_errors"], n_distinct=r["n_distinct"],
        infra_kinds=",".join(sorted({e["kind"] for e in errs if e.get("infra")})) or None,
        state="failure" if st and failed(st) else ("success" if st else "unknown"))
    names: dict[int, str] = {}

    def nid(name):
        i = name_id(name)
        if name:
            names[i] = name
        return i
    wag = [dict(test_id=tid, wagon_id=w["wagon_id"], subwagon_id=w.get("subwagon_id") or 0,
                workflow_id=nid(w.get("workflow_name") or "")) for w in wagons if w.get("wagon_id") is not None]
    wag = list({(x["wagon_id"], x["subwagon_id"], x["workflow_id"]): x for x in wag}.values())
    metrics = [dict(test_id=tid, workflow_id=nid(d["workflow"]), device_id=nid(d["device"]),
                    wagon_id=wf_wagon.get(d["workflow"]), cpu_ms=_ms(d["cpu_s"]), pss_kb=_kb(d["pss_bytes"]))
               for d in devs]   # per DPL device, from status.json; per workflow: hlperf.py
    metrics = list({(x["workflow_id"], x["device_id"]): x for x in metrics}.values())
    errors = []
    seen = set()
    for e in errs:
        k = (e.get("log") or "stdout", e["sig_hash"])
        if k in seen:
            continue
        seen.add(k)
        dev = device_of(e.get("text") or "", e.get("sig_exact")) or e.get("device")
        errors.append(dict(
            test_id=tid, log=k[0], sig_hash=e["sig_hash"], rank=e["rank"], device_id=nid(dev),
            kind=e["kind"], infra=bool(e["infra"]), fatal=bool(e["fatal"]), class_hash=e["class_hash"],
            file=e.get("file"), count=e["count"], line=e.get("line")))   # the text: once, in hl_signatures
    return {"test": test, "wagons": wag, "metrics": metrics, "errors": errors, "names": names}


def _kb(v):
    v = _num(v)
    return None if v is None else int(round(v / 1024))


def _ms(v):
    v = _num(v)
    return None if v is None else int(round(v * 1000))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
