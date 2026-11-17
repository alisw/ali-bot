"""ALICE pull requests and their human discussion -> SQLite + sqlite-vec (prs.db).

The third flow, next to commits_flow.py. runner.sh runs it as

    COCOINDEX_DB=<root>/flows/prs/state FLOW_DB=<root>/flows/prs/prs.db \
        cocoindex update -f prs_flow.py

Each pass has two halves, both in app_main:

1. Sync (plain Python, no cocoindex): fetch the PRs updated since the last
   pass from GitHub GraphQL into a fetch cache, FETCH_DB, which lives INSIDE
   the state dir (state/github/fetch.db). It is therefore tarred, snapshotted
   and restored together with the LMDB state, and the state+db pairing rule
   covers it too. A fetch cache that is lost would leave the flow mounting no
   PRs, and cocoindex would then delete every row: it must never be separated
   from the state it was built with.
2. Index: one memoized component per selected PR, keyed by (repo, number) and
   called with the fingerprint of its cached JSON, so an unchanged PR is a memo
   hit and a changed one is rebuilt as a whole (edited and deleted comments
   disappear with it).

Selection is by the PR's authorAssociation, evaluated by GitHub at query time:
OWNER, MEMBER, COLLABORATOR and CONTRIBUTOR are indexed. FIRST_TIME_CONTRIBUTOR,
FIRST_TIMER, NONE, MANNEQUIN and deleted (ghost) authors are skipped and
recorded; on every pass one PR per distinct skipped author is re-checked
(batched through nodes(ids:)), and once that author qualifies all their skipped
PRs in the repo are fetched again and indexed, without the PRs being updated.
PRs opened by bots are recorded as such and never re-checked.

Environment (beyond commits_flow.py's FLOW_DB, COMMITS_MIRRORS, OPENAI_*,
EMBED_*, COCOINDEX_MAX_INFLIGHT_COMPONENTS, DIFF_EMBED_SINCE):
    PRS_REPOS             comma list owner/repo (default: COMMITS_REPOS without branches)
    GITHUB_GRAPHQL_URL    e.g. http://127.0.0.1:<port>/github/graphql (security-proxy route)
    GITHUB_TOKEN          the proxy's gate token for the github route
    PROXY_SOCK, PROXY_TOKEN   if set, a 401 re-resolves the (daily-rotating) gate token
    PRS_FETCH_BUDGET_S    wall-clock budget of the sync half per pass (default 1200)
    PRS_MAX_PAGES         PR list pages per pass, all repos together (default 0: no cap)
    PRS_PAGE_SIZE         PRs per GraphQL page (default 25)
    PRS_NESTED            first: of the nested comments and reviews (default 50)
    PRS_NESTED_THREADS    first: of the nested review threads (default 10)
    PRS_RATE_RESERVE      stop (or wait for the reset) below this many points (default 500)
    PRS_OVERLAP_S         incremental overlap on updatedAt (default 900)
    PRS_DIFFS             1 = embed the diff of open and closed-unmerged PRs (default 1)
    PRS_BOT_LOGINS        extra comma list of bot logins (case-insensitive)
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
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, AsyncIterator

import cocoindex as coco
from cocoindex.connectors import sqlite
from cocoindex.ops.litellm import LiteLLMEmbedder
from numpy.typing import NDArray

# embedtext.py (the embeddinggemma task prefixes) ships next to this file.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import embedtext  # noqa: E402
import metrics  # noqa: E402

# Embedding requests, texts and latency of this run, to MONIT (metrics.py);
# nothing without OTLP_METRICS_URL.
metrics.install_litellm("prs")

FLOW_DB = os.environ.get("FLOW_DB", "prs.db")
STATE_DIR = pathlib.Path(os.environ.get("COCOINDEX_DB", "."))
FETCH_DB = pathlib.Path(os.environ.get("PRS_FETCH_DB", STATE_DIR / "github" / "fetch.db"))
MIRRORS = pathlib.Path(os.environ.get("COMMITS_MIRRORS", "mirrors"))
REPOS = [s.split(":")[0] for s in os.environ.get(
    "PRS_REPOS", os.environ.get(
        "COMMITS_REPOS", "AliceO2Group/AliceO2,AliceO2Group/O2Physics,AliceO2Group/O2DPG,"
                         "alisw/alidist,alisw/ali-bot")).split(",") if s]
GRAPHQL_URL = os.environ.get("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql")
DIFF_EMBED_SINCE = os.environ.get("DIFF_EMBED_SINCE", "2025-01-01")
DIFFS = os.environ.get("PRS_DIFFS", "1") == "1"
EMBED_MODEL = os.environ.get("EMBED_MODEL", "openai/embeddinggemma-300m")
MAX_INFLIGHT_COMPONENTS = int(os.environ.get("COCOINDEX_MAX_INFLIGHT_COMPONENTS", "16"))
EMBED_MAX_INFLIGHT_REQUESTS = int(os.environ.get("EMBED_MAX_INFLIGHT_REQUESTS", "4"))
EMBED_TIMEOUT_S = float(os.environ.get("EMBED_TIMEOUT_S", "300"))
FETCH_BUDGET_S = float(os.environ.get("PRS_FETCH_BUDGET_S", "1200"))
PAGE_SIZE = int(os.environ.get("PRS_PAGE_SIZE", "25"))
MAX_PAGES = int(os.environ.get("PRS_MAX_PAGES", "0"))
NESTED = int(os.environ.get("PRS_NESTED", "50"))
# Threads cost (threads x THREAD_COMMENTS) points: first: 50 made a page of 25
# PRs cost 13 points on AliceO2, 10 brings it to ~4. More are paged in after.
NESTED_THREADS = min(NESTED, int(os.environ.get("PRS_NESTED_THREADS", "10")))
THREAD_COMMENTS = 20  # comments per review thread; longer threads are cut (rare)
RATE_RESERVE = int(os.environ.get("PRS_RATE_RESERVE", "500"))
OVERLAP_S = int(os.environ.get("PRS_OVERLAP_S", "900"))

QUALIFY = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}
# Who posts on these repos besides humans (checked on recent PRs): alibuild is a
# *User* (MEMBER) that posts CI error reports and auto-approvals; the O2 linter
# posts through github-actions (a Bot). __typename == "Bot" covers the rest.
BOT_LOGINS = {s.lower() for s in ["alibuild", "github-actions", "ALICE-CI-bot", "dependabot",
                                  *os.environ.get("PRS_BOT_LOGINS", "").split(",")] if s}

# Same text caps as commits_flow.py: embeddinggemma-300m answers HTTP 400 above
# 2048 tokens; prose and logs run ~3-4 chars/token.
MAX_EMBED_CHARS = 5000
CHUNK_CHARS = 4400          # discussion text per chunk, leaving room for the prefix
MAX_BODY_CHARS = 20000      # stored body; the first ~5k are the PR's own embedding
MAX_BODY_CHUNKS = 3         # extra embeddings for the rest of a long body
MAX_DISCUSSION_CHUNKS = 30
MAX_DIFF_CHUNKS = 40
MAX_CHUNKS_PER_FILE = 4
SKIP_DIFF_PATHS = re.compile(r"(\.(root|pdf|png|svg|json|csv|dat|txt)$)|(/data/)|(^\.github/)")
HTML_COMMENT = re.compile(r"<!--.*?(-->|$)", re.S)

EMBEDDER = coco.ContextKey[LiteLLMEmbedder]("aigw_embedder", detect_change=True)
DB = coco.ContextKey[sqlite.ManagedConnection]("prs_sqlite")
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def log(*a):
    print("prs:", *a, file=sys.stderr, flush=True)


# ================================================================ GitHub GraphQL
_COMMENT = "databaseId author { __typename login } createdAt lastEditedAt isMinimized body"
_REVIEW = "databaseId author { __typename login } state createdAt submittedAt body"
_THREAD = (f"path line originalLine isOutdated comments(first: {THREAD_COMMENTS}) "
           "{ totalCount nodes { " + _COMMENT + " } }")
_PAGE = "totalCount pageInfo { hasNextPage endCursor }"
_PR = f"""
fragment PR on PullRequest {{
  id number title url state merged isDraft createdAt updatedAt closedAt mergedAt
  baseRefName headRefName headRefOid authorAssociation author {{ __typename login }} body
  comments(first: $n) {{ {_PAGE} nodes {{ {_COMMENT} }} }}
  reviews(first: $n) {{ {_PAGE} nodes {{ {_REVIEW} }} }}
  reviewThreads(first: $nt) {{ {_PAGE} nodes {{ {_THREAD} }} }}
}}"""
_RL = "rateLimit { cost remaining resetAt }"
Q_LIST = f"""query PRList($owner: String!, $name: String!, $first: Int!, $after: String, $n: Int!, $nt: Int!) {{
  {_RL}
  repository(owner: $owner, name: $name) {{
    pullRequests(first: $first, after: $after, orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
      pageInfo {{ hasNextPage endCursor }} nodes {{ ...PR }} }} }} }}""" + _PR
Q_NODES = f"""query PRNodes($ids: [ID!]!, $n: Int!, $nt: Int!) {{
  {_RL} nodes(ids: $ids) {{ ... on PullRequest {{ ...PR }} }} }}""" + _PR
Q_ASSOC = f"""query PRAssoc($ids: [ID!]!) {{
  {_RL} nodes(ids: $ids) {{ ... on PullRequest {{ id updatedAt authorAssociation author {{ __typename login }} }} }} }}"""
Q_MORE = {  # the rest of a nested connection that overflowed its first page
    conn: f"""query PRMore_{conn}($id: ID!, $after: String, $n: Int!) {{
  {_RL} node(id: $id) {{ ... on PullRequest {{ {conn}(first: $n, after: $after) {{ {_PAGE} nodes {{ {fields} }} }} }} }} }}"""
    for conn, fields in (("comments", _COMMENT), ("reviews", _REVIEW), ("reviewThreads", _THREAD))
}


class OutOfBudget(Exception):
    """Time or rate budget used up: stop syncing for this pass, resume next pass."""


class GitHub:
    """GraphQL through the job's security-proxy github route, as ci-mcp does:
    the token sent is the proxy's gate token, swapped for the PAT upstream."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.token = os.environ.get("GITHUB_TOKEN", "")
        self.cost = 0
        self.requests = 0
        self.pages = 0

    def _refresh_token(self) -> bool:
        sock, helper = os.environ.get("PROXY_SOCK"), os.environ.get("PROXY_TOKEN")
        if not (sock and helper):
            return False
        try:
            self.token = subprocess.run([sys.executable, helper, sock, "github", "token"], check=True,
                                        capture_output=True, text=True, timeout=30).stdout.strip()
            return True
        except (subprocess.SubprocessError, OSError):
            return False

    def _sleep_until(self, t: float, why: str):
        wait = max(1.0, t - time.time())
        if time.time() + wait > self.deadline:
            raise OutOfBudget(f"{why}; reset in {wait:.0f}s is past this pass's budget")
        log(f"{why}; sleeping {wait:.0f}s")
        time.sleep(wait)

    def query(self, q: str, **variables) -> dict:
        op = re.match(r"query (\w+)", q).group(1)
        refreshed = False
        for attempt in range(8):
            if time.time() > self.deadline:
                raise OutOfBudget("fetch budget used up")
            req = urllib.request.Request(
                GRAPHQL_URL, data=json.dumps({"query": q, "operationName": op, "variables": variables}).encode(),
                headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                         "User-Agent": "cocoindex-prs"})
            self.requests += 1
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    body, headers = json.load(r), r.headers
            except urllib.error.HTTPError as e:
                h = e.headers
                if e.code == 401 and not refreshed and self._refresh_token():
                    refreshed = True
                    continue
                if e.code in (403, 429) and (h.get("retry-after") or h.get("x-ratelimit-remaining") == "0"):
                    # Secondary limit (retry-after) or primary limit exhausted.
                    until = (time.time() + int(h["retry-after"])) if h.get("retry-after") \
                        else int(h.get("x-ratelimit-reset", time.time() + 60))
                    self._sleep_until(until, f"{op}: HTTP {e.code} rate limited")
                    continue
                if e.code in (500, 502, 503, 504):  # GitHub times out heavy queries
                    log(f"{op}: HTTP {e.code}, retrying")
                    time.sleep(min(60, 2 ** attempt))
                    continue
                raise RuntimeError(f"{op}: HTTP {e.code}: {e.read()[:300]!r}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                log(f"{op}: {e}, retrying")
                time.sleep(min(60, 2 ** attempt))
                continue
            errs = body.get("errors") or []
            if any(e.get("type") == "RATE_LIMITED" for e in errs):
                self._sleep_until(int(headers.get("x-ratelimit-reset") or time.time() + 60), f"{op}: RATE_LIMITED")
                continue
            data = body.get("data") or {}
            # NOT_FOUND inside nodes(ids:) is a deleted PR or repo: a null in the
            # list, not a reason to fail the batch.
            if errs and not all(e.get("type") == "NOT_FOUND" for e in errs):
                raise RuntimeError(f"{op}: {json.dumps(errs)[:400]}")
            rl = data.get("rateLimit") or {}
            self.cost += rl.get("cost", 0)
            if rl and rl.get("remaining", RATE_RESERVE) < RATE_RESERVE:
                reset = datetime.fromisoformat(rl["resetAt"].replace("Z", "+00:00")).timestamp()
                self._sleep_until(reset, f"rate limit {rl['remaining']} < reserve {RATE_RESERVE}")
            return data
        raise RuntimeError(f"{op}: giving up after retries")


# ================================================================ fetch cache
def open_fetch_db() -> sqlite3.Connection:
    FETCH_DB.parent.mkdir(parents=True, exist_ok=True)
    # Rollback journal, not WAL: the file is tarred between passes and must be
    # complete on its own.
    c = sqlite3.connect(FETCH_DB, isolation_level=None)
    c.executescript("""
      CREATE TABLE IF NOT EXISTS pr(
        repo TEXT, number INTEGER, id TEXT, updated_at TEXT, author TEXT, assoc TEXT,
        status TEXT,        -- index | skip | bot
        fp TEXT, data TEXT, PRIMARY KEY (repo, number));
      CREATE TABLE IF NOT EXISTS scan(
        repo TEXT PRIMARY KEY,
        watermark TEXT,     -- newest updatedAt of the last complete scan
        floor TEXT,         -- stop condition of the scan in progress
        scan_hi TEXT,       -- newest updatedAt seen by the scan in progress
        after TEXT);        -- endCursor of the scan in progress (NULL: none running)
    """)
    return c


def classify(pr: dict) -> str:
    a = pr.get("author")
    if not a or a.get("login") == "ghost":
        return "skip"  # deleted account: can never qualify, never re-checked
    if a.get("__typename") == "Bot" or a["login"].lower() in BOT_LOGINS:
        return "bot"
    return "index" if pr.get("authorAssociation") in QUALIFY else "skip"


def fingerprint(pr: dict) -> str:
    return hashlib.sha256(json.dumps(pr, sort_keys=True).encode()).hexdigest()[:32]


def complete(gh: GitHub, pr: dict) -> dict:
    """Page through any nested connection that did not fit in its first page."""
    for conn in ("comments", "reviews", "reviewThreads"):
        c = pr[conn]
        while c["pageInfo"]["hasNextPage"]:
            more = gh.query(Q_MORE[conn], id=pr["id"], after=c["pageInfo"]["endCursor"],
                            n=NESTED_THREADS if conn == "reviewThreads" else 100)
            nxt = (more.get("node") or {}).get(conn)
            if not nxt:
                break
            c["nodes"] += nxt["nodes"]
            c["pageInfo"] = nxt["pageInfo"]
        c.pop("pageInfo", None)
    return pr


def store(db: sqlite3.Connection, repo: str, pr: dict) -> bool:
    """Upsert one PR; True if it changed."""
    fp = fingerprint(pr)
    old = db.execute("SELECT fp FROM pr WHERE repo = ? AND number = ?", (repo, pr["number"])).fetchone()
    if old and old[0] == fp:
        return False
    a = pr.get("author") or {}
    db.execute("INSERT OR REPLACE INTO pr VALUES (?,?,?,?,?,?,?,?,?)",
               (repo, pr["number"], pr["id"], pr["updatedAt"], a.get("login"),
                pr.get("authorAssociation"), classify(pr), fp, json.dumps(pr)))
    return True


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scan_repo(gh: GitHub, db: sqlite3.Connection, repo: str) -> dict:
    """Newest-updated first, down to the last watermark minus the overlap.

    Cursors are keyset ("cursor:v2:" + updatedAt + id), so a scan interrupted
    by the budget, a timeout or a crash resumes exactly where it stopped. A PR
    updated while a scan is in progress moves ahead of it, past scan_hi, and is
    picked up by the next scan, whose floor is scan_hi minus the overlap."""
    owner, name = repo.split("/")
    row = db.execute("SELECT watermark, floor, scan_hi, after FROM scan WHERE repo = ?", (repo,)).fetchone()
    watermark, floor, scan_hi, after = row or (None, None, None, None)
    if after is None:  # start a new scan
        floor = (_iso(datetime.fromisoformat(watermark.replace("Z", "+00:00")) - timedelta(seconds=OVERLAP_S))
                 if watermark else None)
        scan_hi = None
    stats = {"pages": 0, "seen": 0, "changed": 0, "resumed": after is not None}
    while True:
        if MAX_PAGES and gh.pages >= MAX_PAGES:
            raise OutOfBudget(f"PRS_MAX_PAGES={MAX_PAGES} reached")
        gh.pages += 1
        data = gh.query(Q_LIST, owner=owner, name=name, first=PAGE_SIZE, after=after, n=NESTED, nt=NESTED_THREADS)
        conn = data["repository"]["pullRequests"]
        done = not conn["pageInfo"]["hasNextPage"]
        db.execute("BEGIN")
        for pr in conn["nodes"]:
            if floor and pr["updatedAt"] < floor:
                done = True
                break
            scan_hi = max(scan_hi or "", pr["updatedAt"])
            stats["seen"] += 1
            old = db.execute("SELECT updated_at FROM pr WHERE repo = ? AND number = ?",
                             (repo, pr["number"])).fetchone()
            if old and old[0] == pr["updatedAt"]:
                continue  # the overlap window: already have it, do not page its comments again
            stats["changed"] += store(db, repo, complete(gh, pr))
        after = None if done else conn["pageInfo"]["endCursor"]
        if done:
            watermark = scan_hi or watermark
        db.execute("INSERT OR REPLACE INTO scan VALUES (?,?,?,?,?)", (repo, watermark, floor, scan_hi, after))
        db.execute("COMMIT")
        stats["pages"] += 1
        if done:
            return stats


def recheck_skipped(gh: GitHub, db: sqlite3.Connection) -> int:
    """One PR per distinct (repo, author) among the skipped ones: GitHub
    evaluates authorAssociation when asked, so a first-timer whose PR has since
    been merged comes back as CONTRIBUTOR. Their skipped PRs are then fetched
    again in full. Ghost authors (NULL) cannot change and are not asked about."""
    probes = db.execute("""SELECT repo, author, max(id) FROM pr WHERE status = 'skip' AND author IS NOT NULL
                           AND author != 'ghost' GROUP BY repo, author""").fetchall()
    qualified = []
    for i in range(0, len(probes), 100):
        batch = probes[i:i + 100]
        nodes = gh.query(Q_ASSOC, ids=[p[2] for p in batch])["nodes"]
        for (repo, author, _), n in zip(batch, nodes):
            if n and n.get("authorAssociation") in QUALIFY:
                qualified.append((repo, author))
    n_new = 0
    for repo, author in qualified:
        ids = [r[0] for r in db.execute("SELECT id FROM pr WHERE repo = ? AND author = ? AND status = 'skip'",
                                        (repo, author))]
        for i in range(0, len(ids), PAGE_SIZE):
            nodes = gh.query(Q_NODES, ids=ids[i:i + PAGE_SIZE], n=NESTED, nt=NESTED_THREADS)["nodes"]
            db.execute("BEGIN")
            for pr in filter(None, nodes):
                n_new += store(db, repo, complete(gh, pr)) and classify(pr) == "index"
            db.execute("COMMIT")
        log(f"{repo}: {author} now qualifies; {len(ids)} skipped PR(s) fetched again")
    return n_new


def sync() -> None:
    gh = GitHub(time.time() + FETCH_BUDGET_S)
    db = open_fetch_db()
    try:
        for repo in REPOS:
            try:
                st = scan_repo(gh, db, repo)
                log(f"{repo}: {st['pages']} page(s), {st['seen']} PR(s) seen, {st['changed']} changed"
                    + (" (resumed)" if st["resumed"] else ""))
            except OutOfBudget as e:
                log(f"{repo}: {e}; the scan resumes next pass")
                if db.in_transaction:
                    db.execute("ROLLBACK")
                break
        else:
            try:
                n = recheck_skipped(gh, db)
                if n:
                    log(f"{n} previously skipped PR(s) now indexed")
            except OutOfBudget as e:
                log(f"re-check: {e}")
                if db.in_transaction:
                    db.execute("ROLLBACK")
    except Exception as e:  # GitHub down: index what the cache already holds
        log(f"sync failed, indexing the cached PRs: {type(e).__name__}: {e}")
        if db.in_transaction:
            db.execute("ROLLBACK")
    finally:
        log(f"github: {gh.requests} request(s), {gh.cost} rate-limit point(s)")
        db.close()


# ================================================================ git (diffs)
def mirror(repo: str) -> pathlib.Path:
    return MIRRORS / (repo.replace("/", "__") + ".git")


def diff_eligible(pr: dict) -> bool:
    return DIFFS and not pr["merged"] and pr["updatedAt"][:10] >= DIFF_EMBED_SINCE


# The same test in SQL, so a pass does not parse every cached PR's JSON.
_DIFF_ELIGIBLE_SQL = ("status = 'index' AND NOT json_extract(data, '$.merged') "
                      "AND substr(updated_at, 1, 10) >= ?")


def fetch_pull_heads() -> dict[tuple[str, int], bool]:
    """Fetch +refs/pull/<n>/head of every diff-eligible PR whose head commit is
    missing from the mirror (merged PRs are in the commits flow already).
    Returns {(repo, number): head present}."""
    want: dict[str, list[tuple[int, str]]] = defaultdict(list)
    db = open_fetch_db()
    for repo, number, head in db.execute(
            f"SELECT repo, number, json_extract(data, '$.headRefOid') FROM pr WHERE {_DIFF_ELIGIBLE_SQL}",
            (DIFF_EMBED_SINCE,)):
        if mirror(repo).is_dir():
            want[repo].append((number, head))
    db.close()
    present = {}

    def have(repo, shas):
        out = subprocess.run(["git", "--git-dir", str(mirror(repo)), "cat-file", "--batch-check"],
                             input="".join(s + "^{commit}\n" for s in shas), capture_output=True,
                             text=True, env=GIT_ENV).stdout.splitlines()
        return [not line.endswith("missing") for line in out]

    for repo, prs in want.items():
        missing = [(n, s) for (n, s), ok in zip(prs, have(repo, [s for _, s in prs])) if not ok]
        for i in range(0, len(missing), 100):
            specs = [f"+refs/pull/{n}/head:refs/pull/{n}/head" for n, _ in missing[i:i + 100]]
            r = subprocess.run(["git", "--git-dir", str(mirror(repo)), "fetch", "-q", "--no-write-fetch-head",
                                "origin", *specs], capture_output=True, text=True, env=GIT_ENV)
            if r.returncode:  # one vanished ref fails the batch: retry singly
                for s in specs:
                    subprocess.run(["git", "--git-dir", str(mirror(repo)), "fetch", "-q", "--no-write-fetch-head",
                                    "origin", s], capture_output=True, env=GIT_ENV)
        if missing:
            log(f"{repo}: fetched {len(missing)} PR head(s) into the mirror")
        for (n, _), ok in zip(prs, have(repo, [s for _, s in prs])):
            present[(repo, n)] = ok
    return present


async def git(repo: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "--git-dir", str(mirror(repo)), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=GIT_ENV)
    out, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {err.decode(errors='replace')}")
    return out.decode("utf-8", errors="replace")


def split_patch(patch: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for block in re.split(r"(?m)^(?=diff --git )", patch):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", block)
        if m:
            files[m.group(2)] = block
    return files


# ================================================================ text
def human(a: dict | None) -> bool:
    return bool(a) and a.get("__typename") != "Bot" and a.get("login", "").lower() not in BOT_LOGINS


def clean(text: str | None) -> str:
    """PR templates are full of <!-- ... --> instructions to the author."""
    text = HTML_COMMENT.sub("", text or "").replace("\r\n", "\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def discussion(pr: dict) -> tuple[list[tuple[str, str | None, str | None, int | None, str, str]], int]:
    """Chunks of human discussion, (kind, authors, path, line, created_at, text),
    and the number of human comments they hold.

    Issue comments and review bodies are packed in time order, several short
    ones per chunk; each review thread is its own chunk (split if long). One
    item never exceeds CHUNK_CHARS: a pasted log is cut, not spread over chunks."""
    def item(c, prefix=""):
        t = clean(c.get("body"))
        if not t or c.get("isMinimized") or not human(c.get("author")):
            return None
        head = f"@{c['author']['login']} ({(c.get('submittedAt') or c['createdAt'])[:10]}){prefix}: "
        if len(head) + len(t) > CHUNK_CHARS:
            t = t[:CHUNK_CHARS - len(head) - 20] + " [...truncated]"
        return c["author"]["login"], c.get("submittedAt") or c["createdAt"], head + t

    groups = []  # (kind, path, line, [items])
    for kind, nodes in (("comment", pr["comments"]["nodes"]), ("review", pr["reviews"]["nodes"])):
        items = [x for x in (item(c, f" [{c['state'].lower()}]" if kind == "review" else "") for c in nodes) if x]
        groups.append((kind, None, None, sorted(items, key=lambda x: x[1])))
    for t in pr["reviewThreads"]["nodes"]:
        items = [x for x in map(item, t["comments"]["nodes"]) if x]
        if items:
            groups.append(("thread", t["path"], t.get("line") or t.get("originalLine"), items))

    out, n_items = [], sum(len(g[3]) for g in groups)
    for kind, path, line, items in groups:
        hdr = f"{path}:{line or '?'}\n" if kind == "thread" else ""
        cur: list = []
        for it in items + [None]:
            if cur and (it is None or len(hdr) + sum(len(x[2]) + 2 for x in cur) + len(it[2]) > CHUNK_CHARS):
                authors = ",".join(dict.fromkeys(x[0] for x in cur))[:200]
                out.append((kind, authors, path, line, cur[0][1], hdr + "\n\n".join(x[2] for x in cur)))
                cur = []
            if it is not None:
                cur.append(it)
    return out[:MAX_DISCUSSION_CHUNKS], n_items


# ================================================================ rows
@dataclass
class PR:
    repo: str
    number: int
    title: str
    author: str | None
    author_association: str
    state: str  # open | closed | merged
    draft: bool
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    merged_at: datetime | None
    base: str
    head: str
    head_sha: str
    url: str
    body: str  # HTML comments stripped, capped at MAX_BODY_CHARS
    n_comments: int  # human discussion items indexed
    embedding: Annotated[NDArray, EMBEDDER]  # of repo#number: title + body


@dataclass
class PRChunk:
    repo: str
    number: int
    kind: str  # body (continuation of a long body) | comment | review | thread | diff
    chunk: int
    author: str | None  # comma list when a chunk packs several comments
    path: str | None
    line: int | None
    created_at: datetime
    text: str
    embedding: Annotated[NDArray, EMBEDDER]


@dataclass
class PRRepo:
    repo: str
    fetched_through: str | None  # updatedAt watermark of the last complete scan
    scan_in_progress: bool  # bootstrap (or a long catch-up) not finished yet
    indexed: int
    skipped: int  # first-timers, NONE, ghosts: re-checked every pass (ghosts excepted)
    bots: int


@dataclass
class Tables:
    prs: object
    chunks: object


# ================================================================ components
_fetch: sqlite3.Connection | None = None


def _load(repo: str, number: int) -> dict:
    global _fetch
    if _fetch is None:
        _fetch = sqlite3.connect(f"file:{FETCH_DB}?mode=ro", uri=True, check_same_thread=False)
    return json.loads(_fetch.execute("SELECT data FROM pr WHERE repo = ? AND number = ?",
                                     (repo, number)).fetchone()[0])


@coco.fn(memo=True)
async def process_pr(item: tuple[int, str, bool], repo: str, tables: Tables) -> None:
    """One component per PR. The fingerprint of its cached JSON (and whether its
    head commit is in the mirror) is in the arguments, so an unchanged PR is a
    memo hit, and a changed PR declares its rows again from scratch: rows it no
    longer declares (an edited or deleted comment) are deleted by cocoindex.
    Embeddings of unchanged texts are themselves memo hits."""
    number, _fp, has_head = item
    pr = _load(repo, number)
    embedder = coco.use_context(EMBEDDER)
    title, body = pr["title"].strip(), clean(pr["body"])[:MAX_BODY_CHARS]
    head = f"{repo}#{number}: {title}\n\n"
    first = MAX_EMBED_CHARS - len(head)
    state = "merged" if pr["merged"] else pr["state"].lower()
    disc, n_items = discussion(pr)
    pieces = [("body", None, None, None, pr["createdAt"], body[first + i * CHUNK_CHARS:first + (i + 1) * CHUNK_CHARS])
              for i in range(MAX_BODY_CHUNKS) if len(body) > first + i * CHUNK_CHARS] + disc

    if has_head and diff_eligible(pr):
        try:
            base = (await git(repo, "merge-base", f"refs/heads/{pr['baseRefName']}", pr["headRefOid"])).strip()
            patch = await git(repo, "diff", "--no-renames", "--no-color", base, pr["headRefOid"])
        except RuntimeError as e:  # base branch gone, history rewritten: no diff
            log(f"{repo}#{number}: no diff: {str(e)[:200]}")
            patch = ""
        nd = 0
        for path, fdiff in split_patch(patch).items():
            if SKIP_DIFF_PATHS.search(path) or "Binary files" in fdiff[:500]:
                continue
            for i in range(min(MAX_CHUNKS_PER_FILE, -(-len(fdiff) // CHUNK_CHARS))):
                if nd < MAX_DIFF_CHUNKS:
                    pieces.append(("diff", None, path, None, pr["updatedAt"],
                                   fdiff[i * CHUNK_CHARS:(i + 1) * CHUNK_CHARS]))
                    nd += 1

    vecs = await asyncio.gather(
        embedtext.embed_doc(embedder, (head + body)[:MAX_EMBED_CHARS]),
        *(embedtext.embed_doc(embedder, f"{repo}#{number} {title}\n{kind}{' ' + path if path else ''}: {text}"[:MAX_EMBED_CHARS])
          for kind, _, path, _, _, text in pieces))
    a = pr.get("author") or {}
    tables.prs.declare_row(row=PR(
        repo, number, title, a.get("login"), pr["authorAssociation"], state, pr["isDraft"],
        _ts(pr["createdAt"]), _ts(pr["updatedAt"]), _ts(pr["closedAt"]), _ts(pr["mergedAt"]),
        pr["baseRefName"], pr["headRefName"], pr["headRefOid"], pr["url"], body, n_items, vecs[0]))
    seq: dict[str, int] = defaultdict(int)
    for (kind, author, path, line, created, text), vec in zip(pieces, vecs[1:]):
        seq[kind] += 1
        tables.chunks.declare_row(row=PRChunk(repo, number, kind, seq[kind], author, path, line,
                                              _ts(created), text, vec))


@coco.fn(memo=True)
async def process_month(items: tuple[tuple[int, str, bool], ...], repo: str, tables: Tables) -> None:
    """PRs grouped by (repo, month created), as commits_flow groups commits:
    only months with a changed PR descend to per-PR components."""
    await coco.mount_each(process_pr, [(n, (n, fp, h)) for n, fp, h in items], repo, tables)


@coco.lifespan
async def coco_lifespan(builder: coco.EnvironmentBuilder) -> AsyncIterator[None]:
    builder.provide(EMBEDDER, LiteLLMEmbedder(
        EMBED_MODEL, max_inflight_requests=EMBED_MAX_INFLIGHT_REQUESTS, timeout=EMBED_TIMEOUT_S))
    with sqlite.managed_connection(FLOW_DB, load_vec=True) as conn:
        builder.provide(DB, conn)
        yield


@coco.fn
async def app_main() -> None:
    S = sqlite.TableSchema
    prs = await sqlite.mount_table_target(DB, "prs", await S.from_class(PR, primary_key=["repo", "number"]))
    chunks = await sqlite.mount_table_target(
        DB, "pr_chunks", await S.from_class(PRChunk, primary_key=["repo", "number", "kind", "chunk"]))
    repos = await sqlite.mount_table_target(DB, "pr_repos", await S.from_class(PRRepo, primary_key=["repo"]))
    tables = Tables(prs, chunks)

    await asyncio.to_thread(sync)
    heads = await asyncio.to_thread(fetch_pull_heads) if DIFFS else {}
    db = open_fetch_db()
    try:
        for repo in REPOS:
            months: dict[str, list[tuple[int, str, bool]]] = defaultdict(list)
            counts = defaultdict(int)
            for number, status, fp, created in db.execute(
                    "SELECT number, status, fp, json_extract(data, '$.createdAt') FROM pr WHERE repo = ?", (repo,)):
                counts[status] += 1
                if status == "index":
                    months[created[:7]].append((number, fp, heads.get((repo, number), False)))
            sc = db.execute("SELECT watermark, after FROM scan WHERE repo = ?", (repo,)).fetchone() or (None, None)
            repos.declare_row(row=PRRepo(repo, sc[0], sc[1] is not None or sc[0] is None,
                                         counts["index"], counts["skip"], counts["bot"]))
            await coco.mount_each(
                coco.component_subpath("repo", repo), process_month,
                [(m, tuple(sorted(v))) for m, v in sorted(months.items())], repo, tables)
    finally:
        db.close()


app = coco.App(
    coco.AppConfig(name="AlicePRs", max_inflight_components=MAX_INFLIGHT_COMPONENTS),
    app_main,
)
