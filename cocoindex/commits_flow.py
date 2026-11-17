"""ALICE commit history -> SQLite + sqlite-vec (commits.db), with CocoIndex 1.2.

One flow, one output database, one LMDB state directory. runner.sh runs it as

    COCOINDEX_DB=<root>/flows/commits/state FLOW_DB=<root>/flows/commits/commits.db \
        cocoindex update -f commits_flow.py

Environment:
    COCOINDEX_DB          LMDB state dir (local disk, one writer). Pairs with FLOW_DB:
                          the two must always come from the same snapshot.
    FLOW_DB               output SQLite file (default: commits.db)
    COMMITS_MIRRORS       dir of <owner>__<repo>.git bare blobless mirrors
    COMMITS_REPOS         comma list owner/repo:branch (default: the five ALICE repos)
    COMMITS_SINCE         optional ISO date: ignore older commits (smoke tests)
    DIFF_EMBED_SINCE      ISO date: embed diff chunks only for commits newer than this
    OPENAI_API_BASE       e.g. http://127.0.0.1:<port>/aigw/v1   (security-proxy route)
    OPENAI_API_KEY        the proxy's gate token for the aigw route
    EMBED_MODEL           default openai/embeddinggemma-300m
    COCOINDEX_MAX_INFLIGHT_COMPONENTS   default 16 (see below)
    EMBED_MAX_INFLIGHT_REQUESTS         default 4  (requests of <=64 texts each)
    EMBED_TIMEOUT_S                     default 300 (per request, retries included)

Throughput knobs. Concurrency into aigw is min(components in flight, requests in
flight x 64 texts): COCOINDEX_MAX_INFLIGHT_COMPONENTS bounds how many commits are
processed at once (each runs 2-3 git subprocesses -- lazy blob fetches on a
blobless mirror -- and up to ~40 embeds), EMBED_MAX_INFLIGHT_REQUESTS how many
batched HTTP requests hit aigw at once. Neither is part of any memo key, so both
can be tuned between passes without invalidating a single cached embedding.

The endpoint and key are deliberately NOT passed to LiteLLMEmbedder: its memo key
is (model, kwargs), so a per-allocation proxy port or a daily-rotating gate token
in kwargs would invalidate every cached embedding on every run. litellm reads
them from OPENAI_API_BASE / OPENAI_API_KEY instead -- and prefers OPENAI_BASE_URL
over OPENAI_API_BASE, so runner.sh unsets that one.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, AsyncIterator

import cocoindex as coco
from cocoindex.connectors import sqlite
from cocoindex.ops.litellm import LiteLLMEmbedder
from numpy.typing import NDArray

# embedtext.py (the embeddinggemma task prefixes) ships next to this file.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import embedtext  # noqa: E402

FLOW_DB = os.environ.get("FLOW_DB", "commits.db")
MIRRORS = pathlib.Path(os.environ.get("COMMITS_MIRRORS", "mirrors"))
REPOS = [
    tuple(s.split(":"))
    for s in os.environ.get(
        "COMMITS_REPOS",
        "AliceO2Group/AliceO2:dev,AliceO2Group/O2Physics:master,"
        "AliceO2Group/O2DPG:master,alisw/alidist:master,alisw/ali-bot:master",
    ).split(",")
]
SINCE = os.environ.get("COMMITS_SINCE")  # e.g. 2025-01-01
DIFF_EMBED_SINCE = os.environ.get("DIFF_EMBED_SINCE", "2025-01-01")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "openai/embeddinggemma-300m")
MAX_INFLIGHT_COMPONENTS = int(os.environ.get("COCOINDEX_MAX_INFLIGHT_COMPONENTS", "16"))
EMBED_MAX_INFLIGHT_REQUESTS = int(os.environ.get("EMBED_MAX_INFLIGHT_REQUESTS", "4"))
EMBED_TIMEOUT_S = float(os.environ.get("EMBED_TIMEOUT_S", "300"))

# embeddinggemma-300m on aigw answers HTTP 400 (ContextWindowExceeded) above
# 2048 tokens, rather than truncating; code diffs run ~3-4 chars/token. Denser
# text (hash-heavy squash logs) is retried shorter by embedtext.embed_doc.
MAX_EMBED_CHARS = 5000
MAX_CHUNKS_PER_FILE = 4
SKIP_DIFF_PATHS = re.compile(r"(\.(root|pdf|png|svg|json|csv|dat|txt)$)|(/data/)|(^\.github/)")

# Key names are load-bearing: they are CocoIndex's stable identity for the
# embedder and for the database whose rows it tracks. Renaming the DB key makes
# CocoIndex drop the tables it tracked under the old name.
EMBEDDER = coco.ContextKey[LiteLLMEmbedder]("aigw_embedder", detect_change=True)
DB = coco.ContextKey[sqlite.ManagedConnection]("commits_sqlite")

# A lazy blob fetch must never sit on a credential prompt.
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


@coco.lifespan
async def coco_lifespan(builder: coco.EnvironmentBuilder) -> AsyncIterator[None]:
    builder.provide(EMBEDDER, LiteLLMEmbedder(
        EMBED_MODEL,
        max_inflight_requests=EMBED_MAX_INFLIGHT_REQUESTS,
        timeout=EMBED_TIMEOUT_S,
    ))
    # load_vec=True: fail loudly if sqlite-vec cannot be loaded (the "auto"
    # default would silently carry on and then refuse the float[768] columns).
    with sqlite.managed_connection(FLOW_DB, load_vec=True) as conn:
        builder.provide(DB, conn)
        yield


# ---------------------------------------------------------------- row types
@dataclass
class Commit:
    repo: str
    sha: str
    parents: str  # space separated
    author_name: str
    author_email: str
    author_date: datetime
    commit_date: datetime
    subject: str
    body: str
    pr_number: int | None
    files_changed: int
    insertions: int
    deletions: int
    embedding: Annotated[NDArray, EMBEDDER]  # of subject + body


@dataclass
class CommitFile:
    repo: str
    sha: str
    path: str
    insertions: int  # -1 for binary
    deletions: int


@dataclass
class DiffChunk:
    repo: str
    sha: str
    path: str
    chunk: int
    commit_date: datetime
    text: str
    embedding: Annotated[NDArray, EMBEDDER]


@dataclass
class Tables:
    commits: object
    files: object
    chunks: object


# ---------------------------------------------------------------- git helpers
def mirror(repo: str) -> pathlib.Path:
    return MIRRORS / (repo.replace("/", "__") + ".git")


async def git(repo: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "--git-dir", str(mirror(repo)), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=GIT_ENV,
    )
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


# ---------------------------------------------------------------- components
@coco.fn(memo=True)
async def process_commit(sha: str, repo: str, tables: Tables) -> None:
    """One component per commit. Inputs are (sha, repo): immutable, so after
    the first successful run this is a memo hit forever, unless this code (or
    the embedder model) changes."""
    meta = await git(repo, "show", "-s", "--format=%H%x00%P%x00%an%x00%ae%x00%aI%x00%cI%x00%B", sha)
    _, parents, an, ae, ad, cd, msg = meta.split("\x00", 6)
    subject, _, body = msg.strip().partition("\n")
    pr = re.search(r"\(#(\d+)\)\s*$", subject)
    # On a blobless mirror this (and the -p below) fetches the commit's blobs
    # from GitHub on demand: one round trip per call.
    numstat = await git(repo, "show", "--format=", "--numstat", "--no-renames", sha)
    ins = dels = nfiles = 0
    for line in numstat.splitlines():
        a, d, path = line.split("\t", 2)
        bin_ = a == "-"
        tables.files.declare_row(row=CommitFile(repo, sha, path, -1 if bin_ else int(a), 0 if bin_ else int(d)))
        ins += 0 if bin_ else int(a)
        dels += 0 if bin_ else int(d)
        nfiles += 1
    embedder = coco.use_context(EMBEDDER)
    commit_date = datetime.fromisoformat(cd)
    text = f"{repo}: {subject}\n\n{body.strip()}"[:MAX_EMBED_CHARS]
    tables.commits.declare_row(row=Commit(
        repo, sha, parents.strip(), an, ae, datetime.fromisoformat(ad), commit_date,
        subject, body.strip(), int(pr.group(1)) if pr else None, nfiles, ins, dels,
        await embedtext.embed_doc(embedder, text),
    ))
    # Diff chunks: only for single-parent commits newer than DIFF_EMBED_SINCE.
    if len(parents.split()) != 1 or cd[:10] < DIFF_EMBED_SINCE:
        return
    patch = await git(repo, "show", "--format=", "-p", "--no-renames", "--no-color", sha)
    pieces = []
    for path, fdiff in split_patch(patch).items():
        if SKIP_DIFF_PATHS.search(path) or "Binary files" in fdiff[:500]:
            continue
        for i in range(min(MAX_CHUNKS_PER_FILE, -(-len(fdiff) // MAX_EMBED_CHARS))):
            pieces.append((path, i, fdiff[i * MAX_EMBED_CHARS:(i + 1) * MAX_EMBED_CHARS]))
    # Concurrent embed() calls are batched by CocoIndex (up to 64 texts/request).
    vecs = await asyncio.gather(*(
        embedtext.embed_doc(embedder, f"{repo} {path}: {subject}\n{piece}"[:MAX_EMBED_CHARS])
        for path, _, piece in pieces))
    for (path, i, piece), vec in zip(pieces, vecs):
        tables.chunks.declare_row(row=DiffChunk(repo, sha, path, i, commit_date, piece, vec))


@coco.fn(memo=True)
async def process_month(shas: tuple[str, ...], repo: str, tables: Tables) -> None:
    """Groups commits by (repo, month) so a run touches ~150 parent memos per
    repo instead of every commit; only the months whose sha list changed
    (normally just the current one) descend to per-commit components."""
    await coco.mount_each(process_commit, [(s, s) for s in shas], repo, tables)


@coco.fn
async def app_main() -> None:
    S = sqlite.TableSchema
    # Plain tables with float[768] columns: sqlite-vec answers KNN by brute
    # force (vec_distance_cosine), ~1e5 rows x 3 KiB -- well under a second.
    # A vec0 virtual table (virtual_table_def=) is the upgrade if that stops
    # being true; it is a schema change, i.e. a rebuild of the table.
    commits = await sqlite.mount_table_target(
        DB, "commits", await S.from_class(Commit, primary_key=["repo", "sha"]))
    files = await sqlite.mount_table_target(
        DB, "commit_files", await S.from_class(CommitFile, primary_key=["repo", "sha", "path"]))
    chunks = await sqlite.mount_table_target(
        DB, "diff_chunks", await S.from_class(DiffChunk, primary_key=["repo", "sha", "path", "chunk"]))
    tables = Tables(commits, files, chunks)

    for repo, branch in REPOS:
        args = ["log", "--format=%H %cI", f"refs/heads/{branch}"]
        if SINCE:
            args.append(f"--since={SINCE}")
        months: dict[str, list[str]] = defaultdict(list)
        for line in (await git(repo, *args)).splitlines():
            sha, cdate = line.split()
            months[cdate[:7]].append(sha)
        await coco.mount_each(
            coco.component_subpath("repo", repo),
            process_month,
            [(m, tuple(sorted(s))) for m, s in sorted(months.items())],
            repo, tables,
        )


# max_inflight_components is set here from the environment, not left to
# CocoIndex's own reading of COCOINDEX_MAX_INFLIGHT_COMPONENTS, so that the
# default when it is unset is 16 and not 1024 (an AppConfig value wins over the
# variable, so this keeps one source of truth).
app = coco.App(
    coco.AppConfig(name="AliceCommits", max_inflight_components=MAX_INFLIGHT_COMPONENTS),
    app_main,
)
