"""s3_scope_violation(): a per-bucket S3 keypair signs only requests for its bucket.

S3 keys reach every bucket of their project, so the proxy is what narrows a
per-bucket keypair to one bucket. Two ways past that are refused: a raw path
whose first segment differs from the decoded one (the keypair is chosen on the
decoded path, the raw one is signed and sent), and an x-amz-copy-source that
reads from another bucket of the project.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import security_proxy as sp
from starlette.datastructures import Headers

failures = []


def check(label, got_problem, want_refused):
    ok = (got_problem is not None) == want_refused
    if not ok:
        failures.append(f"{label}: got {got_problem!r}, want {'refused' if want_refused else 'allowed'}")
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


def scope(raw, headers=(), bucket="smurfs-village", scoped=True):
    return sp.s3_scope_violation(bucket, raw, Headers(raw=[(k.encode(), v.encode()) for k, v in headers]), scoped)


def run():
    check("plain object in the bucket", scope(b"/smurfs-village/board/generations/x"), False)
    check("bucket root (list)", scope(b"/smurfs-village"), False)
    check("percent-encoded key stays in the bucket", scope(b"/smurfs-village/a%2Fb%20c"), False)
    check("no raw path (non-ASGI caller)", scope(None), False)

    # The decoded path said smurfs-village, the wire says something else.
    check("encoded slash in the bucket segment", scope(b"/smurfs-village%2F..%2Falibuild-ac/x"), True)
    check("raw path names another bucket", scope(b"/alibuild-cas/x"), True)

    check("copy within the bucket", scope(b"/smurfs-village/b", [("x-amz-copy-source", "smurfs-village/a")]), False)
    check("copy within the bucket, leading slash and version",
          scope(b"/smurfs-village/b", [("x-amz-copy-source", "/smurfs-village/a?versionId=1")]), False)
    check("copy from another bucket", scope(b"/smurfs-village/b", [("x-amz-copy-source", "alibuild-cas/blob")]), True)
    check("copy from another bucket, url-encoded",
          scope(b"/smurfs-village/b", [("x-amz-copy-source", "alibuild-ac%2Fentry")]), True)
    check("second copy-source header from another bucket",
          scope(b"/smurfs-village/b", [("x-amz-copy-source", "smurfs-village/a"),
                                       ("x-amz-copy-source", "alibuild-ac/e")]), True)

    # A route's default keypair is meant for every bucket: copies across stay allowed.
    check("default keypair may copy across buckets",
          scope(b"/alibuild-repo/b", [("x-amz-copy-source", "alibuild-cas/a")], bucket="alibuild-repo", scoped=False), False)
    check("default keypair still needs raw and decoded to agree",
          scope(b"/alibuild-repo%2Fx/b", bucket="alibuild-repo", scoped=False), True)
    return failures


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
