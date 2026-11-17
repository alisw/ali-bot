"""ALICE CI build logs (s3://alice-build-logs) -> SQLite + sqlite-vec (ci-failures.db).

The fourth flow, after commits_flow.py and prs_flow.py. runner.sh runs it as

    COCOINDEX_DB=<root>/flows/ci-failures/state FLOW_DB=<root>/flows/ci-failures/ci-failures.db \
        cocoindex update -f ci_failures_flow.py

What it answers (through mcp_server.py): is this failure my PR's fault or a
broken build/builder? Has this error been seen before, since when, on which
PRs, checks, hosts? Was it flaky (red, then green on the same sha)? Which
builds of a PR ran where?

The bucket. report-pr-errors uploads, for EVERY build, green or red,
<owner>/<repo>/<pr>/<sha>/<check context, '/'->'_'>/fullLog.txt and pretty.html
(branch builds put the branch name where the PR number goes, plus a "latest"
copy, which is skipped). A rebuild of the same sha and check OVERWRITES the
same key, and the bucket keeps ~8 weeks. So, like the prs flow's fetch cache,
each pass has two halves in app_main:

1. Sync (plain Python, no cocoindex) into a durable extraction cache, CACHE_DB,
   which lives INSIDE the state dir (state/s3/logs.db): tarred, snapshotted and
   restored with the LMDB state, so it obeys the same pairing rule.
   - list the bucket (ListObjectsV2; ~55 pages for ~54k objects, a few seconds);
   - one row per BUILD = (log key, etag) never deleted: a log that expires or is
     overwritten keeps its rows, and an overwritten key (a rebuild) becomes a
     second build of the same key, which is how "red then green" is seen;
   - for each new build, a ranged GET of the first 4 KB of pretty.html gives the
     verdict (<body class="succeeded|failed">), host, start/finish times and
     allocation -- the exact verdict of that build, durable, and 2 KB instead
     of a 5 MB log;
   - logs are downloaded (failed builds first, newest first) within
     CIF_FETCH_BUDGET_S per pass, scrubbed of secrets, and reduced to their
     header and up to CIF_MAX_ERRORS distinct errors by logscan.py (shared with
     ci-mcp), crash stack traces to their fingerprints. Only that extraction is
     kept (in the private cache), never the log; the index rows hold
     signatures and the line of the log each came from (log_url + line), not
     the text. A newer logscan.EXTRACT_VERSION re-reads the logs still there;
   - GitHub, through the job's github route and best effort: per commit, the
     REST status history (/commits/<sha>/statuses: the real check names, how
     many times a check ran on that sha and what each run said, which shows
     flakiness that the overwritten bucket cannot), and per failed PR build
     the PR's changed files (GraphQL, batched), for in_pr_files.
2. Index: one memoized component per build, keyed by build id and called with
   the fingerprint of everything its rows are made of (extraction, verdict,
   GitHub data, attempt number). Builds carry no embeddings. One more component
   per error signature, grouped by hash prefix: only these are embedded, one
   text per DISTINCT signature across all builds, never whole logs.

Environment (beyond commits_flow.py's FLOW_DB, OPENAI_*, EMBED_*,
COCOINDEX_MAX_INFLIGHT_COMPONENTS):
    S3_ENDPOINT_URL       security-proxy base (s3 route), e.g. http://127.0.0.1:<port>
    AWS_ACCESS_KEY_ID     the s3 route's gate token (sent as an unsigned SigV4 key id)
    CIF_BUCKET            default alice-build-logs
    CIF_FETCH_BUDGET_S    wall clock for log downloads per pass (default 1200)
    CIF_FETCH_THREADS     parallel downloads (default 4)
    CIF_EXTRACT_PROCS     extraction processes (default 4; 0 = in the download threads)
    CIF_LIST_PAGE         ListObjectsV2 max-keys (default 1000)
    CIF_MAX_ERRORS        distinct errors kept per build (default 40)
    CIF_SINCE             optional ISO date: ignore builds uploaded before it (smoke runs)
    CIF_MAX_LOGS          optional cap on log downloads per pass (smoke runs, tests)
    CIF_GH_BUDGET_S       wall clock for GitHub per pass (default 600)
    GITHUB_GRAPHQL_URL    e.g. http://127.0.0.1:<port>/github/graphql; REST is the same minus /graphql
    GITHUB_TOKEN          the github route's gate token
    PROXY_SOCK, PROXY_TOKEN   if set, a 401/403 re-resolves the (daily-rotating) gate tokens
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, AsyncIterator

import cocoindex as coco
from cocoindex.connectors import sqlite
from cocoindex.ops.litellm import LiteLLMEmbedder
from numpy.typing import NDArray

# logscan.py ships next to this file in the job (file("../ci-mcp/logscan.py")),
# and lives in ../ci-mcp in the ali-bot checkout.
_HERE = pathlib.Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "ci-mcp"):
    if (_p / "logscan.py").exists():
        sys.path.insert(0, str(_p))
        break
import logscan  # noqa: E402

sys.path.insert(0, str(_HERE))  # embedtext.py ships next to this file
import embedtext  # noqa: E402
import metrics  # noqa: E402

# Embedding requests, texts and latency of this run, plus the sync's counts
# below, to MONIT (metrics.py); nothing without OTLP_METRICS_URL.
METRICS = metrics.install_litellm("ci-failures")


def gauge(name: str, value: float) -> None:
    if METRICS is not None:
        METRICS.gauge(f"cocoindex_cif_{name}", value)

FLOW_DB = os.environ.get("FLOW_DB", "ci-failures.db")
STATE_DIR = pathlib.Path(os.environ.get("COCOINDEX_DB", "."))
CACHE_DB = pathlib.Path(os.environ.get("CIF_CACHE_DB", STATE_DIR / "s3" / "logs.db"))
BUCKET = os.environ.get("CIF_BUCKET", "alice-build-logs")
S3_URL = os.environ.get("S3_ENDPOINT_URL", "").rstrip("/")
LOG_URL_BASE = os.environ.get("CIF_LOG_URL_BASE", "https://ali-ci.cern.ch/alice-build-logs/")
FETCH_BUDGET_S = float(os.environ.get("CIF_FETCH_BUDGET_S", "1200"))
FETCH_THREADS = int(os.environ.get("CIF_FETCH_THREADS", "4"))
EXTRACT_PROCS = int(os.environ.get("CIF_EXTRACT_PROCS", "4"))
LIST_PAGE = int(os.environ.get("CIF_LIST_PAGE", "1000"))
MAX_ERRORS = int(os.environ.get("CIF_MAX_ERRORS", "40"))
SINCE = os.environ.get("CIF_SINCE", "")
MAX_LOGS = int(os.environ.get("CIF_MAX_LOGS", "0"))
GH_BUDGET_S = float(os.environ.get("CIF_GH_BUDGET_S", "600"))
GRAPHQL_URL = os.environ.get("GITHUB_GRAPHQL_URL", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "openai/embeddinggemma-300m")
MAX_INFLIGHT_COMPONENTS = int(os.environ.get("COCOINDEX_MAX_INFLIGHT_COMPONENTS", "16"))
EMBED_MAX_INFLIGHT_REQUESTS = int(os.environ.get("EMBED_MAX_INFLIGHT_REQUESTS", "4"))
EMBED_TIMEOUT_S = float(os.environ.get("EMBED_TIMEOUT_S", "300"))

PRETTY_BYTES = 4096        # the verdict, host, times and allocation are in the first ~3 KB
PRETTY_SKEW_S = 900        # pretty.html and fullLog.txt of one build are uploaded seconds apart
TEXT_CHARS = 1500          # text per error in the extraction cache only (scrubbed, lines clipped)
EMBED_CHARS = 2000
FILES_BATCH = 10           # PRs per GraphQL query for changed files
REPO_PACKAGE = {"AliceO2Group/AliceO2": "O2"}   # aliBuild package of a repo, when not its name
# Part of every build's fingerprint: bump when assemble() changes what a row
# holds, so all builds are re-declared (cheap: no downloads, no embeddings).
ROWS_VERSION = 5

EMBEDDER = coco.ContextKey[LiteLLMEmbedder]("aigw_embedder", detect_change=True)
DB = coco.ContextKey[sqlite.ManagedConnection]("cif_sqlite")


def log(*a):
    print("ci-failures:", *a, file=sys.stderr, flush=True)


def _hash(*parts) -> str:
    return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()[:16]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _proxy_token(route: str) -> str | None:
    sock, helper = os.environ.get("PROXY_SOCK"), os.environ.get("PROXY_TOKEN")
    if not (sock and helper):
        return None
    try:
        return subprocess.run([sys.executable, helper, sock, route, "token"], check=True,
                              capture_output=True, text=True, timeout=30).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


class OutOfBudget(Exception):
    pass


# ================================================================ S3 (read-only)
class S3:
    """GET and ListObjectsV2 on one bucket through the security-proxy s3 route.

    As ci-mcp's s3_get: the proxy signs for real; this side sends an unsigned
    SigV4 header whose key id is the rotating gate token, which is all the proxy
    checks. Only GETs: the flow never writes to the log bucket."""

    NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

    def __init__(self):
        if not S3_URL:
            raise RuntimeError("S3_ENDPOINT_URL is not set")
        self.token = os.environ.get("AWS_ACCESS_KEY_ID", "")
        self.lock = threading.Lock()
        self.requests = 0
        self.bytes = 0

    def _get(self, path: str, query: dict | None = None, rng: str | None = None, timeout=300):
        url = f"{S3_URL}/{BUCKET}/{urllib.parse.quote(path)}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        refreshed = False
        for attempt in range(5):
            h = {"Authorization": f"AWS4-HMAC-SHA256 Credential={self.token}/00000000/default/s3/aws4_request, "
                                  "SignedHeaders=host, Signature=unsigned",
                 "x-amz-content-sha256": "UNSIGNED-PAYLOAD", "Accept-Encoding": "identity",
                 "User-Agent": "cocoindex-ci-failures"}
            if rng:
                h["Range"] = rng
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                    body, headers = r.read(), r.headers
                with self.lock:
                    self.requests += 1
                    self.bytes += len(body)
                return body, headers
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None, None
                if e.code in (401, 403) and not refreshed:
                    tok = _proxy_token("s3")
                    if tok:
                        self.token, refreshed = tok, True
                        continue
                if e.code in (500, 502, 503, 504) and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"S3 GET {path}: HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"S3 GET {path}: {e}") from None
        raise RuntimeError(f"S3 GET {path}: giving up")

    def list(self):
        """Yields (key, etag, size, last_modified ISO) for every object."""
        token = None
        while True:
            q = {"list-type": "2", "max-keys": str(LIST_PAGE)}
            if token:
                q["continuation-token"] = token
            body, _ = self._get("", q, timeout=120)
            x = ET.fromstring(body)
            ns = self.NS if x.tag.startswith(self.NS) else ""
            for c in x.findall(f"{ns}Contents"):
                yield (c.findtext(f"{ns}Key"), (c.findtext(f"{ns}ETag") or "").strip('"'),
                       int(c.findtext(f"{ns}Size") or 0), c.findtext(f"{ns}LastModified"))
            if x.findtext(f"{ns}IsTruncated") != "true":
                return
            token = x.findtext(f"{ns}NextContinuationToken")

    def get(self, key: str, rng: str | None = None) -> tuple[bytes | None, str | None]:
        body, headers = self._get(key, rng=rng)
        return body, (headers.get("ETag", "").strip('"') if headers else None)


# ================================================================ GitHub
class GitHub:
    """REST (status history) and GraphQL (changed files) through the github
    route. Best effort: any failure ends GitHub for this pass, and the builds
    keep whatever the cache already has."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.rest = GRAPHQL_URL[:-len("/graphql")] if GRAPHQL_URL.endswith("/graphql") else GRAPHQL_URL
        self.requests = 0
        self.cost = 0

    def _call(self, url: str, payload: dict | None):
        refreshed = False
        for attempt in range(6):
            if time.time() > self.deadline:
                raise OutOfBudget("GitHub budget used up")
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode() if payload is not None else None,
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                         "Accept": "application/vnd.github+json", "User-Agent": "cocoindex-ci-failures"})
            self.requests += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.load(r), r.headers
            except urllib.error.HTTPError as e:
                h = e.headers
                if e.code == 401 and not refreshed:
                    tok = _proxy_token("github")
                    if tok:
                        self.token, refreshed = tok, True
                        continue
                if e.code == 404 or e.code == 422:
                    return None, h
                if e.code in (403, 429) and (h.get("retry-after") or h.get("x-ratelimit-remaining") == "0"):
                    until = (time.time() + int(h["retry-after"])) if h.get("retry-after") \
                        else int(h.get("x-ratelimit-reset", time.time() + 60))
                    if until > self.deadline:
                        raise OutOfBudget(f"GitHub rate limited until {_iso(until)}")
                    time.sleep(max(1.0, until - time.time()))
                    continue
                if e.code in (500, 502, 503, 504):
                    time.sleep(min(30, 2 ** attempt))
                    continue
                raise RuntimeError(f"GitHub HTTP {e.code}: {e.read()[:200]!r}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                log(f"github: {e}, retrying")
                time.sleep(min(30, 2 ** attempt))
        raise RuntimeError("GitHub: giving up after retries")

    def statuses(self, repo: str, sha: str) -> list[dict] | None:
        """Every status ever posted on the commit, newest first (REST keeps the
        history; GraphQL's status.contexts only has the latest per context)."""
        out = []
        for page in range(1, 6):
            data, _ = self._call(f"{self.rest}/repos/{repo}/commits/{sha}/statuses?per_page=100&page={page}", None)
            if data is None:
                return None if page == 1 else out
            out += [{k: s.get(k) for k in ("context", "state", "created_at", "description")} for s in data]
            if len(data) < 100:
                break
        return compact_statuses(out)

    def pr_files(self, items: list[tuple[str, int]]) -> dict[tuple[str, int], dict]:
        """{(repo, pr): {"head": sha, "files": [...], "total": n}} for up to
        FILES_BATCH PRs in one query; aliases are fixed (r0..), the values are
        variables, so the query text never contains caller data."""
        decl, body, var = [], [], {}
        for i, (repo, pr) in enumerate(items):
            o, n = repo.split("/")
            decl.append(f"$o{i}: String!, $n{i}: String!, $p{i}: Int!")
            body.append(f"r{i}: repository(owner: $o{i}, name: $n{i}) {{ pullRequest(number: $p{i}) {{ "
                        "headRefOid files(first: 100) { totalCount nodes { path } } } }")
            var.update({f"o{i}": o, f"n{i}": n, f"p{i}": pr})
        q = f"query CIFFiles({', '.join(decl)}) {{ rateLimit {{ cost remaining resetAt }} {' '.join(body)} }}"
        data, headers = self._call(GRAPHQL_URL, {"query": q, "operationName": "CIFFiles", "variables": var})
        if data is None:
            return {}
        d = data.get("data") or {}
        self.cost += (d.get("rateLimit") or {}).get("cost", 0)
        errs = [e for e in data.get("errors") or [] if e.get("type") != "NOT_FOUND"]
        if errs:
            raise RuntimeError(f"CIFFiles: {json.dumps(errs)[:300]}")
        out = {}
        for i, it in enumerate(items):
            p = ((d.get(f"r{i}") or {}).get("pullRequest")) or None
            if p:
                f = p.get("files") or {}
                out[it] = {"head": p.get("headRefOid"), "files": [x["path"] for x in f.get("nodes") or []],
                           "total": f.get("totalCount", 0)}
            else:
                out[it] = {"head": None, "files": None, "total": 0}
        return out


# ================================================================ the cache
def open_cache() -> sqlite3.Connection:
    CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
    # Rollback journal, not WAL: the file is tarred between passes.
    c = sqlite3.connect(CACHE_DB, isolation_level=None, check_same_thread=False)
    c.executescript("""
      CREATE TABLE IF NOT EXISTS obj(          -- the bucket as last listed (fullLog + pretty only)
        key TEXT PRIMARY KEY, etag TEXT, size INTEGER, last_modified TEXT, gone INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS build(        -- one per (log key, etag): never deleted
        build_id TEXT PRIMARY KEY, key TEXT, etag TEXT, size INTEGER, last_modified TEXT,
        repo TEXT, ref TEXT, pr INTEGER, sha TEXT, check_dir TEXT,
        pretty TEXT,            -- JSON from pretty.html (verdict, host, times, alloc), NULL: not yet read
        extracted INTEGER DEFAULT 0, -- logscan.EXTRACT_VERSION it was read with; 0: not yet
        header TEXT,            -- JSON from the log's own header
        failed_package TEXT, packages TEXT, n_errors INTEGER, n_distinct INTEGER, no_output INTEGER,
        errors TEXT,            -- JSON list, ranked, at most CIF_MAX_ERRORS
        first_seen TEXT,        -- when this flow first listed it (our clock, not S3's)
        updated_at TEXT);
      CREATE INDEX IF NOT EXISTS build_key ON build(key, last_modified);
      CREATE INDEX IF NOT EXISTS build_sha ON build(repo, sha);
      CREATE TABLE IF NOT EXISTS gh_status(    -- REST status history of one commit
        repo TEXT, sha TEXT, fetched_at TEXT, data TEXT, PRIMARY KEY (repo, sha));
      CREATE TABLE IF NOT EXISTS pr_files(     -- changed files of a PR, fetched for a build's sha
        repo TEXT, pr INTEGER, sha TEXT, head TEXT, files TEXT, total INTEGER, fetched_at TEXT,
        PRIMARY KEY (repo, pr, sha));
      CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    return c


def parse_key(key: str):
    """(repo, ref, pr, sha, check_dir) of a fullLog.txt key, or None."""
    p = key.split("/")
    if len(p) != 6 or p[5] != "fullLog.txt" or p[3] == "latest":
        return None
    return f"{p[0]}/{p[1]}", p[2], int(p[2]) if p[2].isdigit() else None, p[3], p[4]


_TZ = {"UTC": 0, "GMT": 0, "CET": 1, "CEST": 2}


def _pretty_time(s: str | None) -> str | None:
    """'Mon 16 Nov 2026, 09:23:15 CET' -> '2026-11-16T08:23:15Z'."""
    m = re.match(r"\w+ (\d+) (\w+) (\d{4}), (\d\d:\d\d:\d\d) (\w+)", s or "")
    if not m:
        return None
    try:
        t = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)} {m.group(4)}", "%d %b %Y %H:%M:%S")
    except ValueError:
        return None
    off = _TZ.get(m.group(5))
    if off is None:
        return None
    return (t - timedelta(hours=off)).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_pretty(head: bytes) -> dict:
    t = head.decode("utf-8", errors="replace")

    def cell(name):
        m = re.search(r"<th>" + name + r"</th>\s*<td>(.*?)</td>", t, re.S)
        return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None

    v = re.search(r'<body class="(\w+)"', t)
    a = re.search(r"/ui/allocations/([0-9a-f-]{36})", t)
    return {"verdict": {"succeeded": "success", "failed": "failure"}.get(v.group(1)) if v else None,
            "host": cell("Build host"), "started_at": _pretty_time(cell("Started at")),
            "finished_at": _pretty_time(cell("Finished at")), "alloc": a.group(1) if a else None,
            "alibuild": cell("Alibuild version"), "alidist": cell("Alidist version")}


def extract(raw: bytes) -> dict:
    return logscan.extract_log(raw, MAX_ERRORS, TEXT_CHARS)


# ================================================================ sync
def list_bucket(s3: S3, db: sqlite3.Connection) -> dict:
    t0 = time.time()
    seen = {}
    for key, etag, size, lm in s3.list():
        if key.endswith("/fullLog.txt") or key.endswith("/pretty.html"):
            seen[key] = (etag, size, lm)
    db.execute("BEGIN")
    db.execute("CREATE TEMP TABLE IF NOT EXISTS listed(key TEXT PRIMARY KEY)")
    db.execute("DELETE FROM listed")
    db.executemany("INSERT INTO listed VALUES (?)", ((k,) for k in seen))
    db.executemany("INSERT INTO obj(key, etag, size, last_modified, gone) VALUES (?,?,?,?,0) "
                   "ON CONFLICT(key) DO UPDATE SET etag=excluded.etag, size=excluded.size, "
                   "last_modified=excluded.last_modified, gone=0",
                   ((k, *v) for k, v in seen.items()))
    gone = db.execute("UPDATE obj SET gone = 1 WHERE gone = 0 AND key NOT IN (SELECT key FROM listed)").rowcount
    db.execute("INSERT OR REPLACE INTO meta VALUES ('listed_at', ?)", (_iso(time.time()),))
    db.execute("COMMIT")
    log(f"listed {len(seen)} log/pretty objects in {time.time() - t0:.1f}s ({s3.requests} pages); "
        f"{gone} no longer in the bucket (their builds are kept)")
    return seen


def new_builds(db: sqlite3.Connection, seen: dict) -> int:
    """A build row for every (fullLog key, etag) not seen before."""
    rows = []
    for key, (etag, size, lm) in seen.items():
        pk = parse_key(key)
        if not pk or (SINCE and lm[:10] < SINCE):
            continue
        rows.append((_hash(key, etag), key, etag, size, lm, *pk))
    db.execute("BEGIN")
    n = 0
    for r in rows:
        n += db.execute("INSERT OR IGNORE INTO build(build_id, key, etag, size, last_modified, repo, ref, pr, "
                        "sha, check_dir, first_seen, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (*r, _iso(time.time()), _iso(time.time()))).rowcount
    db.execute("COMMIT")
    return n


def read_pretty(s3: S3, db: sqlite3.Connection, seen: dict) -> None:
    """The verdict of each current build without a verdict yet, from the first
    4 KB of its pretty.html -- if that pretty.html belongs to the same upload
    (report-pr-errors writes both within seconds; a pretty.html older than the
    log means the new one is not up yet, so ask again next pass)."""
    todo = []
    for bid, key, lm in db.execute("SELECT build_id, key, last_modified FROM build WHERE pretty IS NULL"):
        cur = seen.get(key)
        pk = key[:-len("fullLog.txt")] + "pretty.html"
        pv = seen.get(pk)
        if not cur or _hash(key, cur[0]) != bid or not pv:
            continue  # an older version of the key, or no pretty.html at all
        if abs((_epoch(pv[2]) or 0) - (_epoch(lm) or 0)) > PRETTY_SKEW_S:
            continue
        todo.append((bid, pk))
    if not todo:
        return
    t0 = time.time()

    def one(item):
        bid, pk = item
        try:
            body, _ = s3.get(pk, f"bytes=0-{PRETTY_BYTES - 1}")
            return bid, parse_pretty(body) if body else None
        except RuntimeError as e:
            log(f"{pk}: {e}")
            return bid, None

    with ThreadPoolExecutor(max(4, FETCH_THREADS * 2)) as ex:
        res = list(ex.map(one, todo))
    db.execute("BEGIN")
    for bid, p in res:
        if p is not None:
            db.execute("UPDATE build SET pretty = ?, updated_at = ? WHERE build_id = ?",
                       (json.dumps(p), _iso(time.time()), bid))
    db.execute("COMMIT")
    log(f"read {len(res)} pretty.html heads in {time.time() - t0:.1f}s")


def fetch_logs(s3: S3, db: sqlite3.Connection, seen: dict) -> None:
    """Download and extract the current logs not extracted yet, or extracted by
    an older logscan.EXTRACT_VERSION: never-read logs before re-reads, failed
    (or unknown) builds before green ones, newest first, until the budget or
    CIF_MAX_LOGS. A log overwritten since it was listed is still extracted, but
    the etag of what was downloaded must match, or it waits for the next pass.
    A log that has left the bucket keeps what an older version extracted."""
    todo = []
    for bid, key, size, lm, pretty, ver in db.execute(
            "SELECT build_id, key, size, last_modified, pretty, extracted FROM build WHERE extracted < ?",
            (logscan.EXTRACT_VERSION,)):
        cur = seen.get(key)
        if not cur or _hash(key, cur[0]) != bid:
            continue  # overwritten or expired before it was ever read: the row keeps its pretty data
        verdict = (json.loads(pretty) or {}).get("verdict") if pretty else None
        todo.append((ver > 0, verdict == "success", lm, bid, key, cur[0], size))
    todo.sort(key=lambda t: t[2], reverse=True)  # newest first,
    todo.sort(key=lambda t: t[:2])               # new before re-read, failed/unknown before green (stable)
    if MAX_LOGS:
        todo = todo[:MAX_LOGS]
    if not todo:
        return
    deadline = time.time() + FETCH_BUDGET_S
    t0, b0, done, lock = time.time(), s3.bytes, [0, 0], threading.Lock()
    # Extraction is CPU-bound pure Python (~8-10 MB/s per core), downloads are
    # not: threads download, a process pool extracts. "spawn": the children
    # import only logscan, never this flow (or cocoindex's runtime threads).
    procs = None
    if EXTRACT_PROCS > 0:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor
        procs = ProcessPoolExecutor(EXTRACT_PROCS, mp_context=multiprocessing.get_context("spawn"))

    def one(item):
        _, _, _, bid, key, etag, _ = item
        if time.time() > deadline:
            return
        try:
            body, got = s3.get(key)
        except RuntimeError as e:
            log(f"{key}: {e}")
            return
        if body is None or (got and got != etag):
            return  # changed under us: the new etag is a new build, next pass
        ex = (procs.submit(logscan.extract_log, body, MAX_ERRORS, TEXT_CHARS).result()
              if procs is not None else extract(body))
        with lock:
            db.execute("UPDATE build SET extracted = ?, header = ?, failed_package = ?, packages = ?, "
                       "n_errors = ?, n_distinct = ?, no_output = ?, errors = ?, updated_at = ? "
                       "WHERE build_id = ?",
                       (logscan.EXTRACT_VERSION, json.dumps(ex["header"]), ex["failed_package"], json.dumps(ex["packages"]),
                        ex["n_errors"], ex["n_distinct"], ex["no_output"], json.dumps(ex["errors"]),
                        _iso(time.time()), bid))
            done[0] += 1
            done[1] += len(body)

    with ThreadPoolExecutor(FETCH_THREADS) as ex:
        list(ex.map(one, todo))
    if procs is not None:
        procs.shutdown()
    dt = max(time.time() - t0, 1e-6)
    left = len(todo) - done[0]
    log(f"extracted {done[0]} log(s), {done[1] / 1e6:.1f} MB in {dt:.1f}s "
        f"({done[1] / 1e6 / dt:.1f} MB/s); {left} left for later passes")
    gauge("logs_extracted", done[0])
    gauge("extract_mb", done[1] / 1e6)
    gauge("extract_mb_per_s", done[1] / 1e6 / dt)
    gauge("logs_pending", left)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('fetch_rate_mb_s', ?)", (f"{done[1] / 1e6 / dt:.2f}",))


def enrich_github(db: sqlite3.Connection) -> None:
    if not GRAPHQL_URL:
        log("GITHUB_GRAPHQL_URL unset: no status history, no PR files this pass")
        return
    gh = GitHub(time.time() + GH_BUDGET_S)
    gauge("github_budget_exhausted", 0)
    n_st = n_pr = 0
    try:
        # Status history: per commit, and again whenever a new build of it has
        # been listed since (a re-run posts a new status).
        want = db.execute("""
            SELECT b.repo, b.sha, max(b.last_modified) AS lm, min(coalesce(json_extract(b.pretty, '$.verdict'),
                   '')) = 'success' AS green
            FROM build b LEFT JOIN gh_status s ON s.repo = b.repo AND s.sha = b.sha
            GROUP BY b.repo, b.sha HAVING s.fetched_at IS NULL OR max(b.first_seen) >= max(s.fetched_at)
            ORDER BY green, lm DESC""").fetchall()
        for repo, sha, _, _ in want:
            st = gh.statuses(repo, sha)
            db.execute("INSERT OR REPLACE INTO gh_status VALUES (?,?,?,?)",
                       (repo, sha, _iso(time.time()), json.dumps(st if st is not None else [])))
            n_st += 1
        # Changed files: only builds that did not pass need them.
        want = db.execute("""
            SELECT DISTINCT b.repo, b.pr, b.sha FROM build b
            LEFT JOIN pr_files f ON f.repo = b.repo AND f.pr = b.pr AND f.sha = b.sha
            WHERE b.pr IS NOT NULL AND f.repo IS NULL
              AND coalesce(json_extract(b.pretty, '$.verdict'), 'failure') != 'success'
            ORDER BY b.last_modified DESC""").fetchall()
        byp: dict[tuple[str, int], list[str]] = defaultdict(list)
        for repo, pr, sha in want:
            byp[(repo, pr)].append(sha)
        keys = list(byp)
        for i in range(0, len(keys), FILES_BATCH):
            got = gh.pr_files(keys[i:i + FILES_BATCH])
            db.execute("BEGIN")
            for k, v in got.items():
                for sha in byp[k]:
                    db.execute("INSERT OR REPLACE INTO pr_files VALUES (?,?,?,?,?,?,?)",
                               (k[0], k[1], sha, v["head"], json.dumps(v["files"]) if v["files"] is not None
                                else None, v["total"], _iso(time.time())))
            db.execute("COMMIT")
            n_pr += len(got)
    except OutOfBudget as e:
        log(f"github: {e}; the rest next pass")
        gauge("github_budget_exhausted", 1)
    except Exception as e:  # GitHub down: index what the cache holds
        log(f"github failed: {type(e).__name__}: {e}")
    finally:
        if db.in_transaction:
            db.execute("ROLLBACK")
        log(f"github: {gh.requests} request(s), {gh.cost} GraphQL point(s); "
            f"{n_st} commit status histories, {n_pr} PR file lists")
        gauge("github_requests", gh.requests)
        gauge("github_points", gh.cost)


def sync() -> None:
    db = open_cache()
    try:
        s3 = S3()
        seen = list_bucket(s3, db)
        n = new_builds(db, seen)
        log(f"{n} new build(s)")
        gauge("new_builds", n)
        read_pretty(s3, db, seen)
        fetch_logs(s3, db, seen)
        log(f"s3: {s3.requests} request(s), {s3.bytes / 1e6:.1f} MB")
    except Exception as e:  # S3 unreachable: index what the cache already holds
        log(f"sync failed, indexing the cached builds: {type(e).__name__}: {e}")
        if db.in_transaction:
            db.execute("ROLLBACK")
    enrich_github(db)
    db.close()


# ================================================================ rows
@dataclass
class CIBuild:
    build_id: str
    repo: str
    pr: int | None
    branch: str | None          # branch builds (daily tags) instead of a PR
    sha: str
    check_name: str             # the GitHub context ("build/O2Physics/o2"), from the status history;
                                # not "check": that is an SQL keyword, and every query would quote it
    check_dir: str              # the key's directory: the context with '/' -> '_'
    host: str | None
    alloc: str | None
    alidist: str | None         # alidist commit used, from the log header (logs since 2026-11 only)
    started_at: str | None      # UTC, from pretty.html
    finished_at: str | None     # UTC from pretty.html, else the log header's builder-local time
    uploaded_at: str            # S3 LastModified (UTC): the reliable clock
    attempt: int                # n-th observed version of this log key (a rebuild overwrites it)
    current: bool               # still the version in the bucket
    gone: bool                  # the key has expired from the bucket (rows kept)
    state: str                  # success | failure | unknown
    state_source: str           # pretty (pretty.html of this build) | github | inferred | none
    gh_state: str | None        # latest status GitHub has for (sha, check)
    gh_runs: int | None         # builds GitHub saw for (sha, check) (pending -> final): >1 = re-run
    gh_failures: int | None     # of which red: >0 with gh_state=success is red-then-green
    failed_package: str | None
    packages: str | None        # comma list of the aliBuild logs in the file, oldest first
    n_errors: int | None        # error lines (NULL: log not extracted yet)
    n_distinct: int | None
    infra_kinds: str | None     # comma list of infra kinds among the errors
    errors_with_file: int | None
    errors_in_pr_files: int | None
    no_output: bool
    extracted: bool
    log_bytes: int
    log_key: str
    log_url: str


@dataclass
class CIError:
    build_id: str
    sig_hash: str
    rank: int                   # 1 = most decisive (ci-mcp's ranking: serious, fatal, late)
    repo: str
    pr: int | None
    sha: str
    check_name: str
    host: str | None
    uploaded_at: str
    state: str
    kind: str
    infra: bool
    fatal: bool
    class_hash: str
    sig_exact: str
    sig_class: str
    file: str | None
    package: str | None         # aliBuild package whose sources the file is in
    section: str | None         # aliBuild log section the error was in
    in_pr_files: int | None     # 1/0: file among the PR's changed files; NULL: unknown
    count: int
    line: int | None            # 1-based line of the log (ci_builds.log_url) it was taken from;
                                # NULL: extracted before lines were kept. The index holds the
                                # signature, never the log text: the log is the pointer's target.


@dataclass
class CISignature:
    sig_hash: str
    class_hash: str
    kind: str
    infra: bool
    sig_exact: str
    sig_class: str
    file: str | None
    embedding: Annotated[NDArray, EMBEDDER]   # of "<kind>: <sig_exact>"


@dataclass
class CIRepo:
    repo: str
    logs_in_bucket: int
    builds: int
    extracted: int
    pending: int                # current logs not extracted yet, or by an older logscan (re-read pending)
    oldest: str | None          # upload time of the oldest / newest build indexed
    newest: str | None


@dataclass
class Tables:
    builds: object
    errors: object
    sigs: object


# ================================================================ assembly
def _norm_ctx(c: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", c)


def _in_pr(err: dict, repo: str, files: list[str] | None, total: int) -> int | None:
    f = err.get("file")
    if not f or files is None:
        return None
    if ":" in f or (err.get("package") and err["package"] != REPO_PACKAGE.get(repo, repo.split("/")[1])):
        return 0  # an installed package, a build dir, or another package's sources
    hit = any(f == p or f.endswith("/" + p) or p.endswith("/" + f) for p in files)
    if hit:
        return 1
    return None if total > len(files) else 0


def compact_statuses(sts: list[dict]) -> list[dict]:
    """Keep what _runs and the verdict need: every pending (a build starting),
    the first final status after each, the newest status per context, and
    every change of final state. The "error" re-posted while a red check waits
    to be rechecked -- 95% of a busy commit's 500 statuses (measured) -- goes."""
    keep, by_ctx = [], defaultdict(list)
    for st in sts:
        by_ctx[st.get("context")].append(st)
    for ctx_sts in by_ctx.values():
        asc = list(reversed(ctx_sts))
        prev = None
        for i, st in enumerate(asc):
            if (st["state"] == "pending" or i == len(asc) - 1 or prev is None
                    or prev["state"] == "pending" or prev["state"] != st["state"]):
                keep.append(st)
            prev = st
    keep.sort(key=lambda st: st.get("created_at") or "", reverse=True)
    return keep


def _runs(mine: list[dict]) -> list[str]:
    """Outcomes of the builds of one (sha, check), oldest first, from its status
    history (newest first, as GitHub returns it). The CI posts far more statuses
    than it runs builds: a red check is re-posted as "error" over and over while
    it waits to be rechecked ("Rechecking since ...", up to hundreds per commit),
    so a run is a "pending" (a build starting) and the first final state after
    it. Without any pending (history cut at 500, or never posted), each change
    of final state counts as one run."""
    asc = list(reversed(mine))
    if any(s["state"] == "pending" for s in asc):
        runs, waiting = [], False
        for s in asc:
            if s["state"] == "pending":
                waiting = True
            elif waiting:
                runs.append(s["state"])
                waiting = False
        return runs
    runs = []
    for s in asc:
        if not runs or runs[-1] != s["state"]:
            runs.append(s["state"])
    return runs


def context_map(c: sqlite3.Connection) -> dict[str, str]:
    """check_dir -> GitHub context, learnt from every status history fetched: the
    key's directory is the context with every '/' (and any other odd character)
    replaced by '_', which cannot be undone by guessing ("build_O2_fullCI_slc9"
    is "build/O2/fullCI_slc9"). Contexts are few and stable, so a commit whose
    own history lacks one still gets the right name."""
    out: dict[str, str] = {}
    for (ctx,) in c.execute("SELECT DISTINCT json_extract(j.value, '$.context') "
                            "FROM gh_status, json_each(gh_status.data) AS j"):
        if ctx:
            out.setdefault(_norm_ctx(ctx), ctx)
    return out


def assemble(b: dict, attempt: int, current: bool, gone: bool, statuses: list | None,
             files: tuple[list[str] | None, int] | None, ctx: str | None = None) -> tuple[dict, list[dict]]:
    """The rows of one build, as plain dicts (also what its fingerprint covers)."""
    pretty = json.loads(b["pretty"]) if b["pretty"] else {}
    hdr = json.loads(b["header"]) if b["header"] else {}
    errs = json.loads(b["errors"]) if b["errors"] else []
    check = ctx or b["check_dir"].replace("_", "/")  # the guess is right for most contexts, not all
    gh_state, gh_runs, gh_fail, gh_near = None, None, None, None
    if statuses:
        mine = [s for s in statuses if _norm_ctx(s.get("context") or "") == b["check_dir"]]
        if mine:
            check = mine[0]["context"]
            fin = [s for s in mine if s["state"] in ("success", "failure", "error")]
            gh_state = fin[0]["state"] if fin else None  # newest first
            runs = _runs(mine)
            gh_runs, gh_fail = len(runs), sum(r != "success" for r in runs)
            lm = _epoch(b["last_modified"]) or 0
            near = [s for s in fin if abs((_epoch(s["created_at"]) or 0) - lm) < PRETTY_SKEW_S]
            gh_near = near[-1]["state"] if near else None  # the first one posted after the upload
    if pretty.get("verdict"):
        state, source = pretty["verdict"], "pretty"
    elif gh_near:
        state, source = ("success" if gh_near == "success" else "failure"), "github"
    elif b["extracted"]:
        state = "failure" if b["no_output"] or any(e["fatal"] for e in errs) else "success"
        source = "inferred"
    else:
        state, source = "unknown", "none"
    flist, ftotal = files if files else (None, 0)
    out_errs = []
    for e in errs:
        out_errs.append(dict(e, in_pr_files=_in_pr(e, b["repo"], flist, ftotal)))
    with_file = sum(1 for e in out_errs if e.get("file"))
    row = dict(
        build_id=b["build_id"], repo=b["repo"], pr=b["pr"], branch=None if b["pr"] is not None else b["ref"],
        sha=b["sha"], check_name=check, check_dir=b["check_dir"], host=pretty.get("host") or hdr.get("host"),
        alloc=pretty.get("alloc") or hdr.get("alloc"), alidist=hdr.get("alidist"),
        started_at=pretty.get("started_at"),
        finished_at=pretty.get("finished_at") or hdr.get("finished_at"),
        uploaded_at=b["last_modified"], attempt=attempt, current=current, gone=gone,
        state=state, state_source=source, gh_state=gh_state, gh_runs=gh_runs, gh_failures=gh_fail,
        failed_package=b["failed_package"],
        packages=",".join(json.loads(b["packages"])) if b["packages"] else None,
        n_errors=b["n_errors"], n_distinct=b["n_distinct"],
        infra_kinds=",".join(sorted({e["kind"] for e in errs if e["infra"]})) or None,
        errors_with_file=with_file if b["extracted"] else None,
        errors_in_pr_files=(sum(1 for e in out_errs if e["in_pr_files"] == 1)
                            if b["extracted"] and flist is not None else None),
        no_output=bool(b["no_output"]), extracted=bool(b["extracted"]), log_bytes=b["size"],
        log_key=b["key"], log_url=LOG_URL_BASE + b["key"])
    return row, out_errs


_cache_ro: sqlite3.Connection | None = None
_ctx_map: dict[str, str] | None = None


def _cache() -> sqlite3.Connection:
    global _cache_ro
    if _cache_ro is None:
        _cache_ro = sqlite3.connect(f"file:{CACHE_DB}?mode=ro", uri=True, check_same_thread=False)
        _cache_ro.row_factory = sqlite3.Row
    return _cache_ro


def _load(bid: str, attempt: int, current: bool, gone: bool):
    global _ctx_map
    c = _cache()
    if _ctx_map is None:
        _ctx_map = context_map(c)
    b = dict(c.execute("SELECT * FROM build WHERE build_id = ?", (bid,)).fetchone())
    st = c.execute("SELECT data FROM gh_status WHERE repo = ? AND sha = ?", (b["repo"], b["sha"])).fetchone()
    pf = None
    if b["pr"] is not None:
        r = c.execute("SELECT files, total FROM pr_files WHERE repo = ? AND pr = ? AND sha = ?",
                      (b["repo"], b["pr"], b["sha"])).fetchone()
        if r:
            pf = (json.loads(r[0]) if r[0] else None, r[1])
    return assemble(b, attempt, current, gone, json.loads(st[0]) if st else None, pf, _ctx_map.get(b["check_dir"]))


# ================================================================ components
@coco.fn(memo=True)
async def process_build(item: tuple[str, str, int, bool, bool], tables: Tables) -> None:
    """One component per build (log key + etag). The fingerprint of everything
    its rows are made of is in the arguments, so an unchanged build is a memo
    hit. No embeddings here: those are per signature."""
    bid, _fp, attempt, current, gone = item
    row, errs = _load(bid, attempt, current, gone)
    tables.builds.declare_row(row=CIBuild(**row))
    for e in errs:
        tables.errors.declare_row(row=CIError(
            build_id=bid, sig_hash=e["sig_hash"], rank=e["rank"], repo=row["repo"], pr=row["pr"],
            sha=row["sha"], check_name=row["check_name"], host=row["host"], uploaded_at=row["uploaded_at"],
            state=row["state"], kind=e["kind"], infra=bool(e["infra"]), fatal=bool(e["fatal"]),
            class_hash=e["class_hash"], sig_exact=e["sig_exact"], sig_class=e["sig_class"],
            file=e.get("file"), package=e.get("package"), section=e.get("section"),
            in_pr_files=e["in_pr_files"], count=e["count"], line=e.get("line")))


@coco.fn(memo=True)
async def process_month(items: tuple[tuple[str, str, int, bool, bool], ...], tables: Tables) -> None:
    """Builds grouped by (repo, upload month): only months with a changed build
    descend to per-build components."""
    await coco.mount_each(process_build, [(it[0], it) for it in items], tables)


@coco.fn(memo=True)
async def process_sig(sig: tuple[str, str, str, int, str, str, str], tables: Tables) -> None:
    """One component per distinct signature seen in a build that did not pass:
    the only embeddings this flow makes."""
    sig_hash, class_hash, kind, infra, exact, cls, file = sig
    vec = await embedtext.embed_doc(coco.use_context(EMBEDDER), f"{kind}: {exact}"[:EMBED_CHARS])
    tables.sigs.declare_row(row=CISignature(sig_hash, class_hash, kind, bool(infra), exact, cls, file or None, vec))


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


def plan() -> tuple[dict, dict, list]:
    """From the cache: per (repo, month) the build items, per hash prefix the
    signature items, and the per-repo summary rows."""
    c = open_cache()
    c.row_factory = sqlite3.Row
    gone = {r[0] for r in c.execute("SELECT key FROM obj WHERE gone = 1")}
    cur = {r[0]: r[1] for r in c.execute("SELECT key, etag FROM obj WHERE gone = 0 AND key LIKE '%/fullLog.txt'")}
    st_fp = {(r[0], r[1]): _hash(r[2]) for r in c.execute("SELECT repo, sha, data FROM gh_status")}
    pf_fp = {(r[0], r[1], r[2]): _hash(r[3], r[4]) for r in c.execute("SELECT repo, pr, sha, files, total FROM pr_files")}
    ctx = context_map(c)
    months: dict[tuple[str, str], list] = defaultdict(list)
    attempt: dict[str, int] = defaultdict(int)
    sigs: dict[str, tuple] = {}
    for b in c.execute("SELECT build_id, key, etag, repo, pr, sha, check_dir, last_modified, pretty, extracted, "
                       "header, errors, failed_package, n_errors FROM build ORDER BY key, last_modified"):
        attempt[b["key"]] += 1
        is_cur = cur.get(b["key"]) == b["etag"]
        fp = _hash(ROWS_VERSION, b["pretty"], b["extracted"], b["header"], b["errors"], b["failed_package"], b["n_errors"],
                   st_fp.get((b["repo"], b["sha"])), pf_fp.get((b["repo"], b["pr"], b["sha"])),
                   ctx.get(b["check_dir"]))
        months[(b["repo"], b["last_modified"][:7])].append(
            (b["build_id"], fp, attempt[b["key"]], is_cur, b["key"] in gone))
        verdict = json.loads(b["pretty"]).get("verdict") if b["pretty"] else None
        if verdict != "success" and b["errors"]:
            for e in json.loads(b["errors"]):
                # The same text can be matched under two kinds in two builds: take
                # the smallest tuple, so the embedded text does not depend on order.
                t = (e["sig_hash"], e["class_hash"], e["kind"], e["infra"], e["sig_exact"], e["sig_class"],
                     e.get("file") or "")
                sigs[e["sig_hash"]] = min(sigs.get(e["sig_hash"], t), t)
    groups: dict[str, list] = defaultdict(list)
    for h, s in sigs.items():
        groups[h[:2]].append(s)
    repos = []
    for r in c.execute("""
            SELECT b.repo, count(*), sum(b.extracted > 0), min(b.last_modified), max(b.last_modified),
                   sum(o.key IS NOT NULL), sum(o.key IS NOT NULL AND b.extracted < ?)
            FROM build b LEFT JOIN obj o ON o.key = b.key AND o.etag = b.etag AND o.gone = 0
            GROUP BY b.repo ORDER BY b.repo""", (logscan.EXTRACT_VERSION,)):
        repos.append(CIRepo(r[0], r[5], r[1], r[2], r[6], r[3], r[4]))
    c.close()
    return months, groups, repos


@coco.fn
async def app_main() -> None:
    S = sqlite.TableSchema
    builds = await sqlite.mount_table_target(DB, "ci_builds", await S.from_class(CIBuild, primary_key=["build_id"]))
    errors = await sqlite.mount_table_target(
        DB, "ci_errors", await S.from_class(CIError, primary_key=["build_id", "sig_hash"]))
    sigs = await sqlite.mount_table_target(
        DB, "ci_signatures", await S.from_class(CISignature, primary_key=["sig_hash"]))
    repos = await sqlite.mount_table_target(DB, "ci_repos", await S.from_class(CIRepo, primary_key=["repo"]))
    tables = Tables(builds, errors, sigs)

    await asyncio.to_thread(sync)
    months, groups, repo_rows = await asyncio.to_thread(plan)
    for r in repo_rows:
        repos.declare_row(row=r)
    log(f"indexing {sum(map(len, months.values()))} build(s) in {len(months)} month group(s), "
        f"{sum(map(len, groups.values()))} distinct failure signature(s)")
    gauge("builds", sum(map(len, months.values())))
    gauge("signatures", sum(map(len, groups.values())))
    await coco.mount_each(
        coco.component_subpath("builds"), process_month,
        [(f"{repo}@{m}", tuple(sorted(v))) for (repo, m), v in sorted(months.items())], tables)
    await coco.mount_each(
        coco.component_subpath("sigs"), process_sig_group,
        [(g, tuple(sorted(v))) for g, v in sorted(groups.items())], tables)


app = coco.App(
    coco.AppConfig(name="AliceCIFailures", max_inflight_components=MAX_INFLIGHT_COMPONENTS),
    app_main,
)
