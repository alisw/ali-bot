"""upstream_url_for(): a route's upstream path is a scope, and `..` must not escape it.

Regression test for the traversal found in 2026-08: with an upstream that carries a
path (`https://alimonitor.cern.ch/hyperloop`), a client could reach the rest of that
host with the proxy's credential attached by sending `../..`. The URL is normalised
downstream (httpx does it before the request goes out) and `..%2f` decodes to the same
segments, so the check has to happen before the join, over the decoded path too.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import security_proxy as sp


def route(upstream):
    return sp.Route(prefix="/x/", upstream=upstream, token="t", name="x")


SCOPED = route("https://alimonitor.cern.ch/hyperloop")   # has a path -> a scope to escape
BARE = route("https://api.github.com")                   # no path -> nothing to escape

CASES = [
    # (route, client path, expected)
    (SCOPED, "report", "allow"),
    (SCOPED, "a/b/c", "allow"),
    (SCOPED, "", "allow"),
    (SCOPED, "a/../b", "allow"),          # resolves back inside the scope
    (SCOPED, "../../admin", "block"),
    (SCOPED, "..", "block"),
    (SCOPED, "a/../../admin", "block"),
    (SCOPED, "..%2F..%2Fadmin", "block"),  # percent-encoded, upper case
    (SCOPED, "..%2f..%2fadmin", "block"),  # percent-encoded, lower case
    (SCOPED, "%2e%2e/admin", "block"),     # encoded dots
    (SCOPED, "..\\..\\admin", "block"),    # backslash separators
    # A path-less upstream has no scope to escape and the host can never change, so `..`
    # there is harmless -- blocking it would only break clients sending odd-but-legal paths.
    (BARE, "../../whatever", "allow"),
    (BARE, "repos/o/r/pulls/1", "allow"),
]


def run():
    failures = []
    for rt, path, want in CASES:
        try:
            sp.upstream_url_for(rt, path)
            got = "allow"
        except sp.PathTraversal:
            got = "block"
        ok = got == want
        if not ok:
            failures.append(f"{path!r} on {rt.upstream}: got {got}, want {want}")
        print(f"  {'ok  ' if ok else 'FAIL'} {want:5} {path!r}")
    return failures


if __name__ == "__main__":
    print(__doc__.splitlines()[0])
    bad = run()
    print(f"{len(CASES) - len(bad)}/{len(CASES)} passed")
    for f in bad:
        print(f"  FAILED: {f}")
    sys.exit(1 if bad else 0)
