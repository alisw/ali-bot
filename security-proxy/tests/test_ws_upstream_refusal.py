"""An upstream that refuses the websocket upgrade is reported, not turned into a reset.

`ws_proxy` used to `await ws.accept(...)` and only then dial upstream, with no
`try` around the dial. Once the client has been told "101 Switching Protocols"
there is no way left to report a failure: the exception escapes an
already-upgraded connection, uvicorn drops the TCP socket with no close frame,
and the client sees `read: connection reset by peer`.

Every distinct upstream failure therefore looked identical -- a 403 from an ACL,
a wrong path, an unreachable host. That cost a wrong diagnosis of
`nomad alloc exec`: it is a plain 403 for a token without `alloc-exec`, and it
was recorded in ci-jobs/ci-linux-x86.nomad as the proxy "resetting the streaming
websocket it needs".

Reproduced at the time with a hand-rolled upgrade against a path that is not a
websocket endpoint at all (`/v1/agent/self`): the proxy answered `101` and then
died, `curl: (52) Empty reply from server`.

So the ordering itself is the contract, and that is what this pins:

    dial upstream  ->  succeeded?  ->  accept the client
                       refused?    ->  close with a code, having never accepted
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import security_proxy as sp

import websockets
from starlette.websockets import WebSocketDisconnect

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


class FakeURL:
    query = ""


class FakeWS:
    """Records whether the client was ever accepted, which is the whole point."""

    def __init__(self, headers=None):
        self.headers = headers or {}
        self.query_params = {}
        self.url = FakeURL()
        self.accepted = False
        self.closed = None
        self.sent_one = False

    async def accept(self, subprotocol=None):
        self.accepted = True

    async def receive_text(self):
        """One frame, then the client hangs up.

        The frame matters: it makes client_to_upstream actually send to a closed
        upstream, which is the ConnectionClosedError that used to escape as a
        traceback. Without it the pump would die on a missing attribute and the
        close-relay assertions would pass for the wrong reason.
        """
        if self.sent_one:
            raise WebSocketDisconnect(code=1000)
        self.sent_one = True
        return "{}"

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


class CaptureLog(logging.Handler):
    """Collects what the proxy logs, since the reason cannot reach the client."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


def run_ws_proxy(ws, path):
    """Drive ws_proxy to completion, returning the WebSocketDisconnect it raised.

    Anything else that escapes is swallowed and reported as None: on the old
    accept-then-dial code the upstream's InvalidStatus propagates out raw, which
    is precisely the defect, and letting it crash the harness would hide the
    assertions that name it.
    """
    try:
        asyncio.run(sp.ws_proxy(ws, path))
    except WebSocketDisconnect as exc:
        return exc
    except Exception as exc:                     # noqa: BLE001 -- see docstring
        print(f"       (escaped raw: {type(exc).__name__}: {exc})")
        return None
    return None


class ClosingUpstream:
    """An upstream that accepts the upgrade, then closes -- as Nomad does when it
    denies an `alloc exec`: 1011 "Permission denied" AFTER the handshake, never a
    pre-upgrade HTTP status."""

    def __init__(self, code, reason):
        self.close_code, self.close_reason = code, reason

    def __await__(self):
        async def go():
            return self
        return go().__await__()

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration          # nothing to relay; the peer closed

    async def send(self, data):
        raise websockets.exceptions.ConnectionClosedError(None, None)

    async def close(self):
        pass


class FakeConnect:
    """A failing stand-in shaped like the real `websockets.connect`.

    That returns an object which is BOTH awaitable and an async context manager.
    Supporting both matters: it is what lets this test be run against the old
    accept-then-dial code and report the ordering as a plain assertion failure
    ("client was NEVER accepted: got True") rather than a TypeError about the
    stub, which would prove nothing.
    """

    def __init__(self, exc):
        self.exc = exc

    def __await__(self):
        async def go():
            raise self.exc
        return go().__await__()

    async def __aenter__(self):
        raise self.exc

    async def __aexit__(self, *exc_info):
        return False


dialled = []


def with_upstream(raising):
    """Swap websockets.connect for something that fails the way we want."""
    def fake_connect(url, **kwargs):
        dialled.append(url)
        return FakeConnect(raising)
    return fake_connect


def run():
    failures.clear()

    route = sp.Route(prefix="/nomad/", upstream="https://nomad.invalid", token="t",
                     name="nomad", websocket=True)
    real_routes, real_ctx, real_connect = sp.ROUTES, sp.ssl_ctx, websockets.connect
    real_master = dict(sp.MASTER)
    sp.ROUTES = [route]
    sp.ssl_ctx = object()          # non-None: the "TLS not provisioned" guard must not fire
    # The gate check has to pass or we never reach the dial. Derive the token the
    # proxy's own way rather than hand-rolling one, so this keeps working if the
    # derivation changes.
    sp.MASTER["current"], sp.MASTER["previous"] = b"test-master-secret", None
    token = sp.service_token("nomad")

    capture = CaptureLog()
    sp.log.addHandler(capture)

    try:
        ws = FakeWS()
        ws.query_params = {"token": token}

        # --- upstream refuses the upgrade (the ACL-denial case) ----------------
        websockets.connect = with_upstream(
            websockets.exceptions.InvalidStatus(FakeResponse(403)))
        exc = run_ws_proxy(ws, "nomad/v1/client/allocation/x/exec")
        check("refusal raises WebSocketDisconnect", exc is not None, True)
        if exc is not None:
            check("refusal close code is 4003", exc.code, 4003)
            check("reason names the upstream status", "403" in (exc.reason or ""), True)
        check("client was NEVER accepted on refusal", ws.accepted, False)
        # The wire cannot carry the reason: raised before accept(), this is not a
        # close frame, so uvicorn answers a bare 500. The log is the only place
        # the upstream status survives -- so assert it is actually there.
        check("upstream status is logged", any("403" in m for m in capture.messages), True)

        # --- upstream unreachable --------------------------------------------
        ws2 = FakeWS()
        ws2.query_params = {"token": token}
        websockets.connect = with_upstream(OSError("nodename nor servname provided"))
        exc2 = run_ws_proxy(ws2, "nomad/v1/client/allocation/x/exec")
        check("unreachable raises WebSocketDisconnect", exc2 is not None, True)
        if exc2 is not None:
            check("unreachable close code is 4004", exc2.code, 4004)
        check("client was NEVER accepted when unreachable", ws2.accepted, False)
        check("unreachable cause is logged",
              any("unreachable" in m for m in capture.messages), True)
        # --- an SSO route puts its captured credential in the upstream QUERY ---
        # Without this case the redaction assertion below is vacuous: the URLs
        # above carry no query at all, so nothing could have leaked.
        sso = sp.Route(prefix="/sso/", upstream="https://sso.invalid", token="t",
                       name="sso", websocket=True, injected_token="SUPERSECRET")
        sp.ROUTES = [sso]
        sp.MASTER["current"] = b"test-master-secret"
        ws3 = FakeWS()
        ws3.query_params = {"token": sp.service_token("sso")}
        websockets.connect = with_upstream(
            websockets.exceptions.InvalidStatus(FakeResponse(500)))
        run_ws_proxy(ws3, "sso/whatever")
        check("the SSO route was really exercised",
              any("500" in m for m in capture.messages), True)
        check("no credential in the log",
              any("SUPERSECRET" in m for m in capture.messages), False)

        # --- the scheme handed to websockets.connect ---------------------------
        # It accepts ONLY ws:// and wss://. A route that is mostly HTTP declares
        # an http(s) upstream and opts in with "websocket": true; unmapped, the
        # dial raised InvalidURI before touching the network -- which is why
        # `nomad alloc exec` never worked through the proxy.
        for upstream, want in (("https://x.invalid", "wss://x.invalid"),
                               ("http://x.invalid", "ws://x.invalid"),
                               ("wss://x.invalid", "wss://x.invalid")):
            r = sp.Route(prefix="/w/", upstream=upstream, token="t", name="w", websocket=True)
            sp.ROUTES = [r]
            w = FakeWS()
            w.query_params = {"token": sp.service_token("w")}
            dialled.clear()
            websockets.connect = with_upstream(OSError("boom"))
            run_ws_proxy(w, "w/path")
            check(f"{upstream} dials as {want}",
                  dialled[0] if dialled else "<never dialled>", want + "/path")
        # --- upstream closes AFTER the upgrade (Nomad's ACL denial) -----------
        upstream = ClosingUpstream(1011, "Permission denied")
        sp.ROUTES = [route]
        w4 = FakeWS()
        w4.query_params = {"token": token}
        websockets.connect = lambda url, **kw: upstream
        run_ws_proxy(w4, "nomad/v1/client/allocation/x/exec")
        check("client IS accepted when the upgrade succeeds", w4.accepted, True)
        check("upstream close code is relayed", w4.closed and w4.closed[0], 1011)
        check("upstream close reason is relayed",
              w4.closed and w4.closed[1], "Permission denied")

        # a code the protocol never puts on the wire must not be forwarded raw
        w5 = FakeWS()
        w5.query_params = {"token": token}
        websockets.connect = lambda url, **kw: ClosingUpstream(1006, "")
        run_ws_proxy(w5, "nomad/v1/client/allocation/x/exec")
        check("1006 is not forwarded verbatim", w5.closed and w5.closed[0], 1011)
    finally:
        sp.log.removeHandler(capture)
        websockets.connect = real_connect
        sp.ROUTES, sp.ssl_ctx = real_routes, real_ctx
        sp.MASTER.update(real_master)

    return list(failures)


if __name__ == "__main__":
    print(__doc__.splitlines()[0])
    bad = run()
    print(f"{'FAILED' if bad else 'passed'}: {len(bad)} failure(s)")
    sys.exit(1 if bad else 0)
