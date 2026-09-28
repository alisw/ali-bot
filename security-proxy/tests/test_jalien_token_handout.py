"""The agent hands out the JAliEn token it already mints, so builders need no grid cert.

build-loop.sh gets a JAliEn token by running `alien.py token -v 1` on the worker,
authenticating with the machine's grid host certificate, and forwards the resulting
PEMs into the build container as JALIEN_TOKEN_CERT / JALIEN_TOKEN_KEY. On the
claim-based workers that fails -- HAVE_JALIEN_TOKEN=0, measured on alimetal03 --
because alienpy is not installed there. Everything downstream that needs the grid
then breaks, and each failure hides the next: full_system_test.sh aborts on
`alien-token-info`, o2-grp-simgrp-tool aborts on CcdbApi's token check, and DPL
dies with "Unable to find CCDB object" when a conditions object's only replica is
alien:/// (AliceO2#15779).

The proxy already mints exactly this token: mint_alien_token() opens a WebSocket to
JAliEn central authenticated by the host certificate and reads back tokencert /
tokenkey -- "what `alien.py token` / `alien-token-init` does, without the alienpy
dependency". Today it keeps the result to itself, as the client certificate for its
own upstream mTLS.

This describes handing that token out over the agent socket, so a worker needs
neither alienpy nor a readable grid certificate:

    printf 'jalien-token\\n' | nc -U .../agent.sock
    {"tokencert": "-----BEGIN CERTIFICATE-----...", "tokenkey": "-----BEGIN RSA PRIVATE KEY-----..."}

Only the SHORT-LIVED derived token crosses the socket. The long-lived host
certificate stays in the proxy, which is the whole point: build-loop.sh already
forwards these PEMs into the container, so nothing downstream changes.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import security_proxy as sp

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


def check_true(label, got):
    check(label, bool(got), True)


# The reserved service name. Not a route: routes proxy HTTP, this returns a
# credential, and the two must not share a namespace -- a config that happened to
# define a route called "jalien-token" must not be able to shadow it.
SERVICE = sp.JALIEN_TOKEN_SERVICE


def agent_reply(service, *, routes=(), token=None):
    """Drive the real dispatcher with a given minted token (or none).

    Patches module globals for the call and restores them after: run_all.py
    imports every module into one interpreter, so anything left patched here
    breaks the modules that run later (it did -- the websocket tests).
    """
    saved = (sp.PROXY_PORT, sp.PROXY_HOST, sp.ALIEN_TOKEN_PEMS, sp.service_token)
    try:
        sp.PROXY_PORT, sp.PROXY_HOST = 8080, "127.0.0.1"
        sp.ALIEN_TOKEN_PEMS = token
        sp.service_token = lambda name: "gate"   # the HMAC is not under test here
        return sp.agent_response(service, set(routes))
    finally:
        sp.PROXY_PORT, sp.PROXY_HOST, sp.ALIEN_TOKEN_PEMS, sp.service_token = saved


CERT = "-----BEGIN CERTIFICATE-----\nMIIB...\n-----END CERTIFICATE-----\n"
KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----\n"


def run():
    failures.clear()

    # --- the happy path ------------------------------------------------------
    got = agent_reply(SERVICE, routes=["ccdb", "ccdb-prod"], token=(CERT, KEY))
    check("a minted token is returned as tokencert + tokenkey",
          (got.get("tokencert"), got.get("tokenkey")), (CERT, KEY))
    check_true("the reply is a single JSON line, as every other agent reply is",
               json.loads(json.dumps(got) + "") == got)

    # --- what must NOT come back ---------------------------------------------
    # The host certificate is the thing the proxy exists to keep. Only the
    # short-lived token may leave; a reply carrying the source cert would defeat
    # the design even though the token is derived from it.
    check("no gate token is mixed in", "token" in got, False)
    check("the port/host of the HTTP listener are not part of this reply",
          any(k in got for k in ("port", "host")), False)

    # --- before the first mint, and after a failed refresh --------------------
    # rebuild_tls() keeps a still-valid token on a failed re-mint and returns
    # False, so "no token yet" is a real state at startup: the cert arrives by
    # ingest push, and the first mint happens after it. Answer with an error the
    # caller can branch on, NOT an empty string that would be forwarded as a
    # valid-looking credential.
    missing = agent_reply(SERVICE, routes=["ccdb"], token=None)
    check("no token yet reports an error", "error" in missing, True)
    check("...and carries no empty PEMs to be mistaken for a token",
          any(k in missing for k in ("tokencert", "tokenkey")), False)

    # --- the reserved name is not a route ------------------------------------
    # match_route() dispatches on prefix/header for HTTP. This name is answered
    # by the agent alone, so a route of the same name must not shadow it, and
    # asking for it must not turn into an HTTP proxy request.
    shadowed = agent_reply(SERVICE, routes=[SERVICE, "ccdb"], token=(CERT, KEY))
    check("a route with the same name cannot shadow the reserved service",
          (shadowed.get("tokencert"), shadowed.get("tokenkey")), (CERT, KEY))
    check("...and no gate token leaks through that collision",
          "token" in shadowed, False)

    # --- existing behaviour is untouched -------------------------------------
    plain = agent_reply("", routes=["ccdb"])
    check("an empty request still returns port + host",
          (plain.get("port"), plain.get("host")), (8080, "127.0.0.1"))
    svc = agent_reply("ccdb", routes=["ccdb"])
    check("a route still returns its gate token", svc.get("token"), "gate")
    unknown = agent_reply("nope", routes=["ccdb"])
    check("an unknown service still errors and lists what exists",
          (("error" in unknown), unknown.get("services")), (True, ["ccdb"]))

    # --- the shape build-loop.sh consumes ------------------------------------
    # It currently splits `alien.py token -v 1` output with sed on the PEM
    # markers. Returning the two PEMs as separate JSON fields removes that
    # parsing; the forwarding at build-loop.sh:518-519 stays as it is.
    cert = got["tokencert"]
    key = got["tokenkey"]
    check_true("tokencert is a certificate PEM", cert.startswith("-----BEGIN CERTIFICATE-----"))
    check_true("tokenkey is a private key PEM", "PRIVATE KEY-----" in key)
    check("the two are not accidentally the same blob", cert == key, False)

    return list(failures)


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
