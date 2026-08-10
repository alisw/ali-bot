"""match_route(): routes sharing one auth header are told apart by their gate token.

The Nomad CLI only ever sends `X-Nomad-Token`, so the read-only `nomad` route and the
attended `nomad-rw` one cannot be split by header name or by path prefix. Matching on
header *presence* alone would always pick whichever route is listed first and answer
401 for the other, silently making the rw route unreachable. The gate token is
per-route (HMAC(master, name)), so it is what disambiguates.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import security_proxy as sp
from starlette.datastructures import Headers

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


def route(name, *, auth_header=None, prefix="/", s3_sign=None):
    return sp.Route(prefix=prefix, upstream="https://example.cern.ch", token="",
                    name=name, auth_header=auth_header, s3_sign=s3_sign)


def matched(path, headers):
    r = sp.match_route(path, Headers(headers))
    return r.name if r else None


def run():
    saved_routes = list(sp.ROUTES)
    try:
        sp.rotate_master()
        sp.ROUTES[:] = [
            route("nomad", auth_header="X-Nomad-Token"),
            route("nomad-rw", auth_header="X-Nomad-Token"),
            route("consul", auth_header="X-Consul-Token"),
            route("ccdb", prefix="/ccdb/"),
        ]
        ro, rw = sp.service_token("nomad"), sp.service_token("nomad-rw")

        # --- the shared header, disambiguated by gate token ------------------
        check("read-only token picks the read-only route",
              matched("v1/nodes", {"X-Nomad-Token": ro}), "nomad")
        check("rw token reaches the rw route despite being listed second",
              matched("v1/jobs", {"X-Nomad-Token": rw}), "nomad-rw")
        check("header name is matched case-insensitively",
              matched("v1/jobs", {"x-nomad-token": rw}), "nomad-rw")

        # --- an unrecognised token falls back to the first candidate ---------
        # It must land on a route (which then answers 401), not on "no route matches",
        # and on the *read-only* one so the message names the least-privileged route.
        check("garbage token still routes, to the first candidate",
              matched("v1/nodes", {"X-Nomad-Token": "nonsense"}), "nomad")

        # --- the previous master stays valid for one rotation window ---------
        stale = sp.service_token("nomad-rw")
        sp.rotate_master()
        check("a token from the previous window still selects its route",
              matched("v1/jobs", {"X-Nomad-Token": stale}), "nomad-rw")
        sp.rotate_master()
        check("two rotations later it no longer does",
              matched("v1/jobs", {"X-Nomad-Token": stale}), "nomad")

        # --- single-header routes are unaffected -----------------------------
        consul = sp.service_token("consul")
        check("an unshared header matches its own route",
              matched("v1/kv/x", {"X-Consul-Token": consul}), "consul")
        check("an unshared header with a bad token still matches it",
              matched("v1/kv/x", {"X-Consul-Token": "nonsense"}), "consul")
        check("a nomad token does not select consul",
              matched("v1/kv/x", {"X-Nomad-Token": sp.service_token("nomad")}), "nomad")

        # --- a header route only wins when its header is actually present ----
        check("no auth header falls through to prefix matching",
              matched("ccdb/x", {}), "ccdb")
        check("an empty auth header is not a match",
              matched("ccdb/x", {"X-Nomad-Token": ""}), "ccdb")
    finally:
        sp.ROUTES[:] = saved_routes
    return failures


if __name__ == "__main__":
    print(__doc__.splitlines()[0])
    bad = run()
    print(f"{'FAILED' if bad else 'passed'}: {len(bad)} failure(s)")
    sys.exit(1 if bad else 0)
