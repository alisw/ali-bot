"""Snapshot / restore / prune for the cocoindex runner.

    s3snap.py restore --root ROOT --flows commits[,prs,ci-failures]
    s3snap.py publish --root ROOT --flows ... --run-id 20261113T201500Z [--keep N]

Layout on the (sticky ephemeral) disk, per flow F with output DB D:
    ROOT/flows/F/state/         COCOINDEX_DB (LMDB: mdb/data.mdb, mdb/lock.mdb)
    ROOT/flows/F/D              the output SQLite file
    ROOT/published.json         manifest of the last snapshot this disk published

Layout in the store (STORE_URL = s3://bucket/prefix  or  file:///dir for tests):
    PREFIX/<run_id>/D                       VACUUM INTO copy, one per flow
    PREFIX/<run_id>/state-F.tar.zst         tar of F's LMDB dir (lock.mdb excluded)
    PREFIX/<run_id>/manifest.json           sizes + sha256 of the above
    PREFIX/latest.json                      copy of the manifest, written LAST

Protocol. publish uploads every file of the run, then the run's manifest, and
only then overwrites latest.json; a reader that starts from latest.json can
therefore never see a run id whose files are still being written. restore
takes a flow's state and DB from the SAME run id (the one latest.json names) or
not at all. prune keeps the newest --keep run ids and, unconditionally, the one
latest.json names.

S3 access goes through the job's security-proxy sidecar: S3_ENDPOINT_URL is the
proxy, AWS_ACCESS_KEY_ID its gate token, AWS_SECRET_ACCESS_KEY anything. The
proxy re-signs with the real keys and only for the buckets in its allowlist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.parse
from pathlib import Path

# flow name -> output DB file name. The single registry of flows; runner.sh's
# FLOWS variable selects which of these run.
FLOW_DBS = {
    "commits": "commits.db",
    # Pull requests and their human discussion. Its state dir also holds the
    # GitHub fetch cache (state/github/fetch.db), so it travels in the state
    # tarball and obeys the same pairing rule.
    "prs": "prs.db",
    # CI build logs (s3://alice-build-logs): one row per build and per error
    # signature. Its state dir also holds the extraction cache
    # (state/s3/logs.db), which outlives the bucket's ~8-week retention, so it
    # travels in the state tarball and obeys the same pairing rule.
    "ci-failures": "ci-failures.db",
}
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z$")


def log(*a):
    print("s3snap:", *a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------ stores
class FileStore:
    """file:///dir -- a local stand-in for the bucket, for tests."""

    def __init__(self, root: str):
        self.root = Path(root)

    def get(self, key):
        p = self.root / key
        return p.read_bytes() if p.exists() else None

    def download(self, key, dest):
        shutil.copyfile(self.root / key, dest)

    def put_file(self, key, src):
        (self.root / key).parent.mkdir(parents=True, exist_ok=True)
        tmp = self.root / (key + ".part")
        shutil.copyfile(src, tmp)
        tmp.rename(self.root / key)

    def put_bytes(self, key, data):
        (self.root / key).parent.mkdir(parents=True, exist_ok=True)
        tmp = self.root / (key + ".part")
        tmp.write_bytes(data)
        tmp.rename(self.root / key)

    def run_ids(self, prefix):
        d = self.root / prefix
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []

    def delete_run(self, prefix, run_id):
        shutil.rmtree(self.root / prefix / run_id)


class S3Store:
    def __init__(self, bucket: str):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["S3_ENDPOINT_URL"],
            aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "unused"),
            config=Config(
                s3={"addressing_style": "path"},
                # As report-pr-errors: no aws-chunked bodies through the proxy.
                request_checksum_calculation="WHEN_REQUIRED",
                response_checksum_validation="WHEN_REQUIRED",
                retries={"max_attempts": 8, "mode": "standard"},
            ),
        )

    def get(self, key):
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def download(self, key, dest):
        self.s3.download_file(self.bucket, key, str(dest))

    def put_file(self, key, src):
        self.s3.upload_file(str(src), self.bucket, key)

    def put_bytes(self, key, data):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data,
                           ContentType="application/json", CacheControl="no-cache")

    def run_ids(self, prefix):
        out = []
        pages = self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix + "/", Delimiter="/")
        for page in pages:
            for cp in page.get("CommonPrefixes", []):
                out.append(cp["Prefix"][len(prefix) + 1:].rstrip("/"))
        return sorted(out)

    def delete_run(self, prefix, run_id):
        pages = self.s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=f"{prefix}/{run_id}/")
        for page in pages:
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys, "Quiet": True})


def open_store():
    """Returns (store, prefix) from STORE_URL."""
    u = urllib.parse.urlparse(os.environ["STORE_URL"])
    prefix = u.path.strip("/") if u.scheme == "s3" else ""
    if u.scheme == "file":
        return FileStore(u.path), "cocoindex"
    if u.scheme == "s3":
        return S3Store(u.netloc), prefix or "cocoindex"
    sys.exit(f"STORE_URL must be s3://bucket/prefix or file:///dir, got {u.geturl()!r}")


# ------------------------------------------------------------------ helpers
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def content_sha256(path: Path) -> str:
    """sha256 of a VACUUM INTO copy with the header's bookkeeping counters
    zeroed: file change counter (24-27), schema cookie (40-43) and
    version-valid-for / SQLite version (92-99). Reopening a restored DB bumps
    the schema cookie once without changing a byte of schema or data, which
    would otherwise read as "changed" and publish an identical snapshot."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        head = bytearray(f.read(100))
        for lo, hi in ((24, 28), (40, 44), (92, 100)):
            head[lo:hi] = bytes(hi - lo)
        h.update(head)
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def zstd_compress(src: Path, dest: Path):
    try:
        import zstandard
    except ImportError:  # local test without the wheel: the CLI does the same
        subprocess.run(["zstd", "-q", "-f", "-T0", "-3", str(src), "-o", str(dest)], check=True)
        return
    with open(src, "rb") as i, open(dest, "wb") as o:
        zstandard.ZstdCompressor(level=3, threads=-1).copy_stream(i, o)


def zstd_decompress(src: Path, dest: Path):
    try:
        import zstandard
    except ImportError:
        subprocess.run(["zstd", "-q", "-f", "-d", str(src), "-o", str(dest)], check=True)
        return
    with open(src, "rb") as i, open(dest, "wb") as o:
        zstandard.ZstdDecompressor().copy_stream(i, o)


def flow_paths(root: Path, flow: str):
    d = root / "flows" / flow
    return d, d / "state", d / FLOW_DBS[flow]


def has_state(state: Path) -> bool:
    # cocoindex 1.2 keeps its LMDB environment in <COCOINDEX_DB>/mdb/.
    return state.is_dir() and any(state.rglob("data.mdb"))


def has_local(root: Path, flow: str) -> tuple[bool, bool]:
    _, state, db = flow_paths(root, flow)
    return has_state(state), db.exists()


# ------------------------------------------------------------------ restore
def restore(root: Path, flows: list[str]) -> int:
    todo = []
    for f in flows:
        st, db = has_local(root, f)
        if st and db:
            log(f"{f}: local state and db present, nothing to restore")
            continue
        if st or db:
            # Half a pair is worse than none: the state would claim rows the DB
            # lacks (or the reverse) and memo hits would never repair it.
            d, _, _ = flow_paths(root, f)
            aside = d.with_name(f"{f}.orphan-{int(time.time())}")
            log(f"{f}: only {'state' if st else 'db'} present; moving {d} aside to {aside}")
            d.rename(aside)
        todo.append(f)
    if not todo:
        return 0

    store, prefix = open_store()
    raw = store.get(f"{prefix}/latest.json")
    if raw is None:
        log(f"no {prefix}/latest.json: first run, starting {todo} from scratch")
        return 0
    man = json.loads(raw)
    run_id = man["run_id"]
    for f in todo:
        ent = man["flows"].get(f)
        if ent is None:
            log(f"{f}: not in snapshot {run_id} (new flow?), starting from scratch")
            continue
        d, state, db = flow_paths(root, f)
        tmp = d.with_name(f"{f}.restoring")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        for kind in ("db", "state"):
            key = f"{prefix}/{run_id}/{ent[kind]['name']}"
            dest = tmp / ent[kind]["name"]
            log(f"{f}: fetching {key} ({ent[kind]['size']} bytes)")
            store.download(key, dest)
            if dest.stat().st_size != ent[kind]["size"] or sha256(dest) != ent[kind]["sha256"]:
                raise SystemExit(f"{f}: {key} does not match the manifest; refusing to restore")
        tarball = tmp / ent["state"]["name"]
        plain = tmp / "state.tar"
        zstd_decompress(tarball, plain)
        with tarfile.open(plain) as t:
            t.extractall(tmp, filter="data")  # yields tmp/state/
        plain.unlink()
        tarball.unlink()
        (tmp / ent["db"]["name"]).rename(tmp / db.name)
        # One rename publishes state + db together: a crash leaves either the
        # old (absent) pair or the complete new one, never half.
        d.parent.mkdir(parents=True, exist_ok=True)
        tmp.rename(d)
        log(f"{f}: restored state + db from run {run_id}")
    # The restored pair is what the bucket already holds: remember that, so the
    # next pass does not re-upload an unchanged DB.
    pub = root / "published.json"
    old = json.loads(pub.read_text()) if pub.exists() else {"flows": {}}
    for f in todo:
        if f in man["flows"]:
            old["flows"][f] = man["flows"][f]
    old["run_id"] = run_id
    pub.write_text(json.dumps(old, indent=1))
    return 0


# ------------------------------------------------------------------ publish
def publish(root: Path, flows: list[str], run_id: str, keep: int, min_interval: int) -> int:
    if not RUN_ID_RE.match(run_id):
        sys.exit(f"bad run id {run_id!r}")
    snap = root / "snap"
    shutil.rmtree(snap, ignore_errors=True)
    snap.mkdir()
    pub = root / "published.json"
    last = json.loads(pub.read_text()) if pub.exists() else {"flows": {}}

    entries, changed = {}, []
    for f in flows:
        _, state, db = flow_paths(root, f)
        if not has_state(state) or not db.exists():
            log(f"{f}: no local state/db yet, not in this snapshot")
            continue
        # VACUUM INTO, never cp: a consistent, compacted, WAL-free copy even if
        # a reader had the file open.
        dbname = FLOW_DBS[f]
        out = snap / dbname
        with sqlite3.connect(db) as c:
            c.execute("VACUUM INTO ?", (str(out),))
        c.close()
        db_sha, db_content = sha256(out), content_sha256(out)
        if last["flows"].get(f, {}).get("db", {}).get("content_sha256") != db_content:
            changed.append(f)
        # The LMDB dir is quiescent (cocoindex has exited), so a plain tar of
        # data.mdb is a consistent copy. lock.mdb is per-process and rebuilt.
        plain = snap / f"state-{f}.tar"
        with tarfile.open(plain, "w") as t:
            t.add(state, arcname="state", filter=lambda ti: None if ti.name.endswith("lock.mdb") else ti)
        tz = snap / f"state-{f}.tar.zst"
        zstd_compress(plain, tz)
        plain.unlink()
        entries[f] = {
            "db": {"name": dbname, "size": out.stat().st_size, "sha256": db_sha,
                   "content_sha256": db_content},
            "state": {"name": tz.name, "size": tz.stat().st_size, "sha256": sha256(tz)},
            "flow_file_sha256": os.environ.get(f"FLOW_SHA_{f.replace('-', '_')}", ""),
            "pass_exit": int(os.environ.get(f"PASS_EXIT_{f.replace('-', '_')}", "-1")),
        }
    if not changed:
        log("no output DB changed since the last snapshot; nothing published")
        shutil.rmtree(snap, ignore_errors=True)
        return 0
    age = time.time() - last.get("published_at", 0)
    if age < min_interval:
        log(f"{changed} changed but the last snapshot is {int(age)}s old (< {min_interval}); deferring")
        shutil.rmtree(snap, ignore_errors=True)
        return 0

    store, prefix = open_store()
    man = {
        "format": 1,
        "run_id": run_id,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "producer": {k: os.environ.get(k, "") for k in ("NOMAD_JOB_NAME", "NOMAD_ALLOC_ID", "NODE_NAME")},
        "cocoindex_version": os.environ.get("COCOINDEX_VERSION", ""),
        "changed": changed,
        "flows": entries,
    }
    for f, ent in entries.items():
        for kind in ("db", "state"):
            key = f"{prefix}/{run_id}/{ent[kind]['name']}"
            log(f"uploading {key} ({ent[kind]['size']} bytes)")
            store.put_file(key, snap / ent[kind]["name"])
    body = json.dumps(man, indent=1).encode()
    store.put_bytes(f"{prefix}/{run_id}/manifest.json", body)
    store.put_bytes(f"{prefix}/latest.json", body)  # the commit point
    log(f"published run {run_id} (changed: {changed})")
    man["published_at"] = time.time()
    pub.write_text(json.dumps(man, indent=1))
    shutil.rmtree(snap, ignore_errors=True)
    prune(store, prefix, keep)
    return 0


def prune(store, prefix: str, keep: int):
    raw = store.get(f"{prefix}/latest.json")
    current = json.loads(raw)["run_id"] if raw else None
    runs = [r for r in store.run_ids(prefix) if RUN_ID_RE.match(r)]
    doomed = [r for r in runs[:-keep] if r != current] if keep > 0 else []
    for r in doomed:
        log(f"pruning run {r}")
        store.delete_run(prefix, r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["restore", "publish"])
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--flows", required=True)
    ap.add_argument("--run-id")
    ap.add_argument("--keep", type=int, default=12)
    ap.add_argument("--min-interval", type=int, default=0,
                    help="seconds; defer a snapshot if the last one is younger")
    a = ap.parse_args()
    flows = [f for f in a.flows.split(",") if f]
    for f in flows:
        if f not in FLOW_DBS:
            sys.exit(f"unknown flow {f!r}")
    if a.cmd == "restore":
        return restore(a.root, flows)
    return publish(a.root, flows, a.run_id, a.keep, a.min_interval)


if __name__ == "__main__":
    sys.exit(main())
