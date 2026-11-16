"""Read side of the cocoindex snapshots: fetch, cache, open read-only, query.

    reader.py sync                                  # refresh the local cache
    reader.py search "fix for TPC calibration crash" [--repo R] [--diffs] [-k 10]
    reader.py like <sha> [-k 10]                    # nearest commits to a commit (offline)
    reader.py sql "select repo, count(*) from commits group by repo"

Store (COCO_STORE, first match wins):
    https://...   public-read bucket, plain GET   e.g. https://s3.cern.ch/<bucket>/cocoindex
    s3://bucket/prefix  through the Mac's security-proxy s3 route (boto3, gate token)
    file:///dir   a local copy (tests)

Protocol: latest.json is read first and names one run id; every file is taken
from that run and checked against the manifest's size + sha256, then renamed
into place. A file whose sha256 already matches is not downloaded again, so an
unchanged DB costs one small GET per check. Writers only ever publish
latest.json after all of a run's files are complete, so this never sees a
half-written snapshot.

Query embeddings (search): OPENAI_API_BASE / OPENAI_API_KEY as for the flow, or
resolved from the Mac's security-proxy aigw route when unset. The model must be
the one the flow used (EMBED_MODEL, default embeddinggemma-300m), or distances
are meaningless.

MCP use: a tool server imports this module, calls sync(max_age=...) at most
every few minutes (cheap: one GET of latest.json), keeps one connect() per
process and reopens it when sync() reports a new run id. Tools map 1:1 onto
search_commits(), search_diffs() and a read-only sql(); connections are opened
mode=ro&immutable=1, so a tool cannot write even if handed a DDL statement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

CACHE = Path(os.environ.get("COCO_CACHE", Path.home() / ".cache" / "cocoindex-reader"))
STORE = os.environ.get("COCO_STORE", "")
PROXY_SOCK = os.environ.get("SECURITY_PROXY_SOCK", "/usr/local/var/run/security-proxy/agent/agent.sock")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "openai/embeddinggemma-300m")

# Secondary indexes for the filters the MCP tools use. cocoindex's SQLite
# target creates none, and they must not go into the indexer's live database
# (SQLite refuses ALTER TABLE DROP COLUMN on an indexed column, which a
# cocoindex schema change may issue), so each reader adds them to its verified
# copy, before the rename() that makes it visible.
INDEXES = {
    "commits.db": ["CREATE INDEX IF NOT EXISTS commits_pr ON commits(repo, pr_number)"],
    "prs.db": ["CREATE INDEX IF NOT EXISTS prs_created ON prs(created_at)",
               "CREATE INDEX IF NOT EXISTS prs_author ON prs(author COLLATE NOCASE, created_at)",
               "CREATE INDEX IF NOT EXISTS prs_state ON prs(state, created_at)"],
    "ci-failures.db": ["CREATE INDEX IF NOT EXISTS ci_errors_sig ON ci_errors(sig_hash, uploaded_at)",
                       "CREATE INDEX IF NOT EXISTS ci_errors_class ON ci_errors(class_hash)",
                       "CREATE INDEX IF NOT EXISTS ci_errors_pr ON ci_errors(repo, pr)",
                       "CREATE INDEX IF NOT EXISTS ci_builds_pr ON ci_builds(repo, pr, uploaded_at)",
                       "CREATE INDEX IF NOT EXISTS ci_builds_sha ON ci_builds(repo, sha, check_dir)"],
}


# ------------------------------------------------------------------ fetching
def _proxy(route: str) -> tuple[str, str]:
    """(base URL, gate token) for a security-proxy route, resolved at call time:
    the port is random and the token rotates daily, so never cache these for long.
    On the Mac the security-proxy-token CLI is on PATH; inside a Nomad allocation
    (no CLI) the agent socket is spoken directly: "<route>\\n" in, one JSON line
    ({"host", "port", "token"}) out -- the same exchange as proxy-token.py."""
    if shutil.which("security-proxy-token"):
        run = lambda *a: subprocess.run(["security-proxy-token", "--socket", PROXY_SOCK, *a],
                                        check=True, capture_output=True, text=True).stdout.strip()
        return run("--addr").rstrip("/"), run(route)
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(PROXY_SOCK)
    s.sendall((route + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    reply = json.loads(buf.decode() or "{}")
    if "token" not in reply:
        raise RuntimeError(f"security-proxy gave no token for route {route!r}")
    return f"http://{reply.get('host', '127.0.0.1')}:{reply['port']}", reply["token"]


class _Store:
    def __init__(self, url: str):
        if not url:
            sys.exit("set COCO_STORE (https://..., s3://bucket/prefix or file:///dir)")
        self.url = url.rstrip("/")
        u = urllib.parse.urlparse(self.url)
        self.s3 = None
        if u.scheme == "s3":
            import boto3
            from botocore.config import Config
            self.bucket, self.prefix = u.netloc, u.path.strip("/") or "cocoindex"
            addr, token = _proxy("s3")
            self.s3 = boto3.client(
                "s3", endpoint_url=addr,
                aws_access_key_id=token, aws_secret_access_key="unused",
                config=Config(s3={"addressing_style": "path"},
                              request_checksum_calculation="WHEN_REQUIRED",
                              response_checksum_validation="WHEN_REQUIRED"))

    def get(self, name: str) -> bytes:
        if self.s3:
            return self.s3.get_object(Bucket=self.bucket, Key=f"{self.prefix}/{name}")["Body"].read()
        req = urllib.request.Request(f"{self.url}/{name}", headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    def download(self, name: str, dest: Path):
        if self.s3:
            self.s3.download_file(self.bucket, f"{self.prefix}/{name}", str(dest))
            return
        with urllib.request.urlopen(f"{self.url}/{name}", timeout=600) as r, open(dest, "wb") as o:
            shutil.copyfileobj(r, o, 1 << 20)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def sync(max_age: float = 0) -> dict:
    """Bring CACHE up to the run latest.json names; returns that manifest.
    With max_age > 0, skip the network entirely if the last check is younger."""
    CACHE.mkdir(parents=True, exist_ok=True)
    local = CACHE / "latest.json"
    if max_age and local.exists() and time.time() - local.stat().st_mtime < max_age:
        return json.loads(local.read_text())
    store = _Store(STORE)
    man = json.loads(store.get("latest.json"))
    run = man["run_id"]
    for flow, ent in man["flows"].items():
        db = ent["db"]
        dest = CACHE / db["name"]
        if dest.exists() and (CACHE / (db["name"] + ".sha256")).exists() and \
                (CACHE / (db["name"] + ".sha256")).read_text() == db["sha256"]:
            continue
        tmp = CACHE / (db["name"] + ".part")
        print(f"fetching {flow}: {run}/{db['name']} ({db['size'] / 1e6:.1f} MB)", file=sys.stderr)
        store.download(f"{run}/{db['name']}", tmp)
        if tmp.stat().st_size != db["size"] or _sha256(tmp) != db["sha256"]:
            tmp.unlink()
            raise RuntimeError(f"{run}/{db['name']} does not match latest.json; not using it")
        if INDEXES.get(db["name"]):
            with sqlite3.connect(tmp) as c:
                for ddl in INDEXES[db["name"]]:
                    c.execute(ddl)
            c.close()
        # rename(): open connections keep reading the old inode until reopened.
        tmp.rename(dest)
        (CACHE / (db["name"] + ".sha256")).write_text(db["sha256"])
    local.write_text(json.dumps(man))
    return man


# ------------------------------------------------------------------ opening
def _ro_uri(path: Path) -> str:
    return f"file:{urllib.parse.quote(str(path))}?mode=ro&immutable=1"


def connect(main: str = "commits.db", attach: dict[str, str] | None = None) -> sqlite3.Connection:
    """Open MAIN read-only with sqlite-vec loaded, and ATTACH each {alias: file}
    that exists in the cache -- e.g. connect(attach={"cf": "ci-failures.db", "pr": "prs.db"}),
    then:

        SELECT p.number, p.title, b.check_name, b.host, b.state
        FROM pr.prs p JOIN cf.ci_builds b ON b.repo = p.repo AND b.pr = p.number
        WHERE p.created_at >= '2026-11-01';"""
    import sqlite_vec

    conn = sqlite3.connect(_ro_uri(CACHE / main), uri=True, check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    for alias, name in (attach or {}).items():
        if (CACHE / name).exists():
            conn.execute(f"ATTACH DATABASE ? AS {alias}", (_ro_uri(CACHE / name),))
    return conn


# ------------------------------------------------------------------ queries
def embed_endpoint() -> tuple[str, str]:
    """(base, key) for query embeddings: OPENAI_API_BASE/OPENAI_API_KEY when both
    are set (tests, a static key), else the security-proxy aigw route."""
    if os.environ.get("OPENAI_API_BASE") and os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_BASE"], os.environ["OPENAI_API_KEY"]
    addr, token = _proxy("aigw")
    return addr + "/aigw/v1", token


def embed_query(text: str, endpoint: tuple[str, str] | None = None) -> list[float]:
    base, key = endpoint or embed_endpoint()
    model = EMBED_MODEL.split("/", 1)[1] if EMBED_MODEL.startswith("openai/") else EMBED_MODEL
    req = urllib.request.Request(
        base.rstrip("/") + "/embeddings",
        data=json.dumps({"model": model, "input": [text]}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["data"][0]["embedding"]


def search_commits(conn, query_vec, k=10, repo=None):
    import sqlite_vec
    q = sqlite_vec.serialize_float32(query_vec) if not isinstance(query_vec, bytes) else query_vec
    return conn.execute(
        """SELECT repo, substr(sha, 1, 10), substr(commit_date, 1, 10), pr_number, subject,
                  round(vec_distance_cosine(embedding, ?), 4) AS d
           FROM commits WHERE (? IS NULL OR repo = ?) ORDER BY d LIMIT ?""",
        (q, repo, repo, k)).fetchall()


def search_diffs(conn, query_vec, k=10, repo=None):
    import sqlite_vec
    q = sqlite_vec.serialize_float32(query_vec) if not isinstance(query_vec, bytes) else query_vec
    return conn.execute(
        """SELECT repo, substr(sha, 1, 10), path, chunk,
                  round(vec_distance_cosine(embedding, ?), 4) AS d
           FROM diff_chunks WHERE (? IS NULL OR repo = ?) ORDER BY d LIMIT ?""",
        (q, repo, repo, k)).fetchall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sync", "search", "like", "sql"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--repo")
    ap.add_argument("--diffs", action="store_true")
    ap.add_argument("--max-age", type=float, default=300, help="seconds between latest.json checks")
    a = ap.parse_args()
    man = sync(0 if a.cmd == "sync" else a.max_age)
    if a.cmd == "sync":
        print(f"run {man['run_id']}: " + ", ".join(man["flows"]))
        return
    conn = connect(attach={"cf": "ci-failures.db", "pr": "prs.db"})
    if a.cmd == "sql":
        for row in conn.execute(a.arg):
            print(*row, sep="\t")
        return
    if a.cmd == "like":
        row = conn.execute("SELECT embedding FROM commits WHERE sha LIKE ? || '%'", (a.arg,)).fetchone()
        if not row:
            sys.exit(f"no commit {a.arg}")
        vec = row[0]
    else:
        vec = embed_query(a.arg)
    for row in (search_diffs if a.diffs else search_commits)(conn, vec, a.k, a.repo):
        print(*row, sep="\t")


if __name__ == "__main__":
    main()
