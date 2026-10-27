"""The client's path is forwarded with its percent-encoding intact.

Starlette hands the catch-all route an already-decoded `path`, so building the
upstream URL from it turned `%2F` inside a segment into a real `/`. The QC
framework stores objects at CCDB paths carrying metadata such as
`qc_task_name=Test%2Fpid3435`; through the proxy that arrived as
`qc_task_name=Test/pid3435`, a bare `pid3435` segment CCDB cannot parse, and
Tomcat answered 400. `%3F` and `%23` would have become a query and a fragment.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import security_proxy as sp

import httpx
from fastapi.testclient import TestClient

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


CCDB = sp.Route(prefix="/ccdb/", upstream="https://ccdb.example.cern.ch", token="", name="ccdb")
NOMAD = sp.Route(prefix="/", upstream="https://nomad.example.cern.ch", token="", name="nomad",
                 auth_header="X-Nomad-Token")
UPLOAD = "qc/TST/MO/Test/pid3435/1/2/ObjectType=TObject/qc_task_name=Test%2Fpid3435/x%3Fy%23z"


def run():
    failures.clear()

    # --- the helper on its own -------------------------------------------
    decoded = UPLOAD.replace("%2F", "/").replace("%3F", "?").replace("%23", "#")
    check("prefix route forwards the raw path minus its prefix",
          sp.client_upstream_path(CCDB, "ccdb/" + decoded, b"/ccdb/" + UPLOAD.encode()), UPLOAD)
    check("header route forwards the full raw path",
          sp.client_upstream_path(NOMAD, "v1/var/a/b", b"/v1/var/a%2Fb"), "v1/var/a%2Fb")
    check("no raw_path falls back to the decoded path",
          sp.client_upstream_path(CCDB, "ccdb/a/b", None), "a/b")
    check("encoded prefix falls back to the decoded path",
          sp.client_upstream_path(CCDB, "ccdb/a/b", b"/cc%64b/a/b"), "a/b")
    check("traversal through raw %2F is still refused",
          _blocked(sp.Route(prefix="/x/", upstream="https://h/scope", token="", name="x"),
                   sp.client_upstream_path(CCDB, "ccdb/../../admin", b"/ccdb/..%2F..%2Fadmin")),
          True)

    # --- end to end through proxy() --------------------------------------
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(201, stream=httpx.ByteStream(b""))

    saved = list(sp.ROUTES), sp.http_client
    try:
        sp.rotate_master()
        sp.ROUTES[:] = [CCDB]
        sp.http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        resp = TestClient(sp.app).post(
            "/ccdb/" + UPLOAD + "?keep=1",
            headers={"Authorization": f"Bearer {sp.service_token('ccdb')}"}, content=b"")
        check("upload through proxy() succeeds", resp.status_code, 201)
        check("upstream receives %2F, %3F and %23 still encoded, query kept",
              seen, ["/" + UPLOAD + "?keep=1"])
    finally:
        sp.ROUTES[:], sp.http_client = saved

    return list(failures)


def _blocked(route, upstream_path):
    try:
        sp.upstream_url_for(route, upstream_path)
        return False
    except sp.PathTraversal:
        return True


if __name__ == "__main__":
    print(__doc__.splitlines()[0])
    bad = run()
    print(f"{'all passed' if not bad else f'{len(bad)} failure(s)'}")
    sys.exit(1 if bad else 0)
