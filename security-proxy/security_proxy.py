#!/usr/bin/env python3
"""Credential proxy for ALICE services.

Accepts incoming HTTP requests with a per-service gate token for authorization,
then forwards them to the appropriate upstream server using a real grid
certificate for TLS client authentication. Each route prefix maps to a different
upstream; its gate token is fetched from a per-user UNIX socket (`security-proxy-token
<service>`) and rotates daily.

Supports both HTTP and WebSocket upstreams.

Credentials can be provided as PEM files (--cert/--key), a PKCS12 file (--p12),
or exported from the macOS Keychain at startup (--keychain-identity).
"""

import argparse
import asyncio
import atexit
import base64
import grp
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import webbrowser
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
import posixpath
from urllib.parse import (urlparse, parse_qs, parse_qsl, quote, unquote, urlencode,
                          urlsplit, urlunsplit)
import httpx
import websockets
from pathlib import Path
from fastapi import FastAPI, Request, Response, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, JSONResponse
import uvicorn


@dataclass
class Route:
    prefix: str
    upstream: str
    token: str
    name: str = ""  # service name; drives the per-service gate token
    websocket: bool = False
    sso: dict | None = None
    injected_token: str | None = None  # set at startup for SSO routes
    ingest_headers: dict | None = None  # header name -> ingest slot providing its value
    auth_header: str | None = None  # read the gate token from this header, not Authorization
    s3_sign: dict | None = None  # S3 SigV4 signer: {access_slot, secret_slot, region, service}
    oauth: dict | None = None  # OAuth2 refresh-token broker: {"accounts": {name -> account}}
    sign: dict | None = None  # local Ed25519 signer (alibuild AC): {"key_slot": <slot>}
    allow_cookies: bool = False  # opt-in only: browser localhost cookies may otherwise leak upstream


@dataclass(frozen=True)
class KeychainIdentity:
    sha1: str
    name: str


# Configured at startup
ROUTES: list[Route] = []
http_client: httpx.AsyncClient = None
ssl_ctx: ssl.SSLContext = None

# Rotating master secret + the random port we bound, handed out per-service via a
# per-user UNIX socket. Each service's gate token is HMAC(master, service), so a
# token minted for one route cannot be replayed against another.
MASTER: dict[str, bytes | None] = {"current": None, "previous": None}
PROXY_PORT: int | None = None
DEFAULT_AGENT_SOCKET = "~/.security-proxy/agent.sock"
DEFAULT_ROTATION_SECONDS = 86400
WS_GATE_SUBPROTOCOL_PREFIX = "security-proxy-token."

# Real upstream secrets (Nomad ACL token, etc.) are pushed in at runtime over the
# write-only ingest socket and held only in memory, keyed by slot name. The proxy
# never reads them from disk or the Keychain.
SLOTS: dict[str, str] = {}
DEFAULT_INGEST_SOCKET = "~/.security-proxy/ingest.sock"

# Attended slots hold high-privilege secrets that must never sit resident in memory:
# they are deliberately NOT filled by `security-proxy-bootstrap`, so their routes stay
# closed until the human runs `security-proxy-unlock <slot>`. That command sources the
# value from a *locked* Keychain, so filling the slot costs a password/Touch ID prompt
# nobody can answer unattended -- an agent running as the same uid can invoke the
# command but cannot satisfy the prompt, which is the only asymmetry that survives
# same-uid access. Once armed the value expires after `ttl` seconds and/or `max_uses`
# requests, so an approved window cannot be ridden indefinitely.
# Config: {"attended_slots": {"<slot>": {"ttl": 300, "max_uses": 1}}}
ATTENDED: dict[str, dict] = {}
SLOT_STATE: dict[str, dict] = {}  # armed attended slot -> {"expires", "uses_left"}
DEFAULT_ATTENDED_TTL = 300
ATTENDED_SWEEP_SECONDS = 15

# When set (config "agent_socket_group" / "ingest_socket_group"), sockets are
# created group-accessible (0660 + that gid) for separate-user daemon deployments.
# Legacy "socket_group" still applies to both sockets when the split keys are absent.
AGENT_SOCKET_GID: int | None = None
INGEST_SOCKET_GID: int | None = None

REQUEST_COOKIE_HEADERS = {"cookie", "cookie2"}
RESPONSE_COOKIE_HEADERS = {"set-cookie", "set-cookie2"}

# Optional ingest slots that supply the client certificate (and key) at runtime, so
# the proxy can hold no cert files on disk. APP_ARGS keeps the parsed args so the TLS
# context/client can be (re)built when those slots arrive.
CERT_SLOT: str | None = None
KEY_SLOT: str | None = None
APP_ARGS = None

# Optional JAliEn token minting. When configured, the grid/host certificate is used
# *only* to obtain a short-lived token certificate from JAliEn central; that token --
# not the long-lived cert -- is then what the proxy presents on mTLS upstream legs
# (e.g. CCDB). Config: {"endpoint": "wss://...", "refresh_seconds": N}.
ALIEN_TOKEN: dict | None = None
DEFAULT_ALIEN_ENDPOINT = "wss://alice-jcentral.cern.ch:8097/websocket/json"
DEFAULT_ALIEN_REFRESH_SECONDS = 43200  # 12h; tokens are typically valid ~24h


def rotate_master() -> None:
    """Generate a fresh master secret, keeping the prior one valid for one window."""
    MASTER["previous"] = MASTER["current"]
    MASTER["current"] = secrets.token_bytes(32)


def derive_token(master: bytes, service: str) -> str:
    """Per-service gate token: urlsafe-base64 of HMAC-SHA256(master, service)."""
    mac = hmac.new(master, service.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def service_token(service: str) -> str:
    return derive_token(MASTER["current"], service)


def gate_ok(presented: str, service: str) -> bool:
    """True if `presented` is this service's current (or previous) gate token."""
    if not presented:
        return False
    pb = presented.encode("utf-8", "ignore")
    for master in (MASTER["current"], MASTER["previous"]):
        if master is not None and secrets.compare_digest(pb, derive_token(master, service).encode()):
            return True
    return False


def match_route(path: str, headers) -> Route | None:
    """Find the route for a request: by a distinctive auth header if present, else
    by URL path prefix (longest first).

    A route with `auth_header` set is matched by the *presence* of that header
    (e.g. X-Nomad-Token / X-Consul-Token / X-Vault-Token), so several such services
    can share one port at the root, disambiguated by which token header is sent.
    An `s3_sign` route is matched by an AWS-style `Authorization` header (the bucket
    name lives in the path, so it can't be matched by prefix). Routes with neither
    are matched by path prefix as before."""
    # 0) S3 signing routes: matched by an AWS-style Authorization header (SigV4 or v2)
    if headers.get("authorization", "")[:3].upper() == "AWS":
        for route in ROUTES:
            if route.s3_sign:
                return route
    # 1) header-matched services (the HashiCorp CLIs, all at /v1/...)
    for route in ROUTES:
        if route.auth_header and headers.get(route.auth_header):
            return route
    # 2) local Ed25519 signing routes: prefix-matched, but the sign endpoint is hit at
    # the *bare* prefix (no trailing slash) while /pubkey is a sub-path, so match both.
    for route in ROUTES:
        if route.sign:
            pfx = route.prefix.strip("/")
            if path == pfx or path.startswith(pfx + "/"):
                return route
    # 3) path-prefix routes (browser / curl)
    for route in ROUTES:
        if (not route.auth_header and not route.s3_sign and not route.sign
                and path.startswith(route.prefix.lstrip("/"))):
            return route
    return None


def presented_token(headers, route: Route, query_token: str = "") -> str:
    """Read the client's gate token from the route's configured location.

    Default is the `Authorization: Bearer <token>` header. A route may set
    `auth_header` to read the gate token from a custom header instead (e.g.
    Nomad's `X-Nomad-Token`). `query_token` is a fallback used by WebSocket
    handshakes, including reserved token subprotocols that are not forwarded."""
    if route.auth_header:
        return headers.get(route.auth_header, "")
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:]
    return query_token


def parse_ws_subprotocols(header: str) -> list[str]:
    """Return the client-requested WebSocket subprotocol tokens."""
    return [s.strip() for s in header.split(",") if s.strip()]


def split_ws_gate_subprotocols(subprotocols: list[str]) -> tuple[str, list[str]]:
    """Extract the proxy gate token from reserved subprotocols and strip it."""
    gate_token = ""
    forwarded = []
    for subprotocol in subprotocols:
        if subprotocol.startswith(WS_GATE_SUBPROTOCOL_PREFIX):
            if not gate_token:
                gate_token = subprotocol[len(WS_GATE_SUBPROTOCOL_PREFIX):]
            continue
        forwarded.append(subprotocol)
    return gate_token, forwarded


def redact_query_for_log(value: str) -> str:
    """Remove query strings from paths before they reach logs."""
    if "?" not in value:
        return value
    path, _query = value.split("?", 1)
    return f"{path}?<redacted>"


class RedactingQueryFilter(logging.Filter):
    """Logging filter that strips query strings from Uvicorn HTTP/WS access paths."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_query_for_log(arg) if isinstance(arg, str) else arg
                                for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact_query_for_log(value) if isinstance(value, str) else value
                           for key, value in record.args.items()}
        return True


def install_log_redaction(log_config: dict) -> None:
    log_config.setdefault("filters", {})["redact_query"] = {"()": RedactingQueryFilter}
    for handler_name in ("default", "access"):
        handler = log_config.get("handlers", {}).get(handler_name)
        if not isinstance(handler, dict):
            continue
        filters = handler.setdefault("filters", [])
        if "redact_query" not in filters:
            filters.append("redact_query")


def validate_absolute_url(parser: argparse.ArgumentParser, value, where: str,
                          allowed_schemes: set[str]) -> str:
    if not isinstance(value, str) or not value:
        parser.error(f"{where} must be a non-empty URL string")
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme not in allowed_schemes:
        allowed = " or ".join(f"{item}://" for item in sorted(allowed_schemes))
        parser.error(f"{where} must use {allowed}")
    if not parts.hostname:
        parser.error(f"{where} must include a host")
    if parts.username or parts.password:
        parser.error(f"{where} must not include userinfo")
    if parts.fragment:
        parser.error(f"{where} must not include a URL fragment")
    return value


class PathTraversal(Exception):
    """Raised when a client path would escape its route's prefix via `..`."""
    def __init__(self, path: str):
        super().__init__(path)
        self.path = path


def upstream_url_for(route: Route, upstream_path: str) -> str:
    """Join the client's path onto the route's upstream, refusing scope escapes.

    A route whose upstream carries a path (`https://alimonitor.cern.ch/hyperloop`) uses
    that path as a *scope*: the gate token for `hyperloop` must not reach the rest of
    the host with the proxy's credential attached. `..` would do exactly that -- the
    URL is normalised downstream (httpx does it before the request goes out), so the
    check has to happen here, over the percent-decoded path as well, since `..%2f`
    decodes to the same segments.

    Upstreams with no path (`https://api.github.com`) have no scope to escape and the
    host can never change, so `..` there is harmless and left alone."""
    scope = urlsplit(route.upstream).path.rstrip("/") + "/"
    for candidate in (upstream_path, unquote(upstream_path)):
        resolved = posixpath.normpath(scope + candidate.replace("\\", "/"))
        if resolved != scope.rstrip("/") and not resolved.startswith(scope):
            raise PathTraversal(upstream_path)
    return f"{route.upstream}/{upstream_path}" if upstream_path else route.upstream


class SlotUnavailable(Exception):
    """Raised when a route needs an ingest slot that has not been provisioned yet."""
    def __init__(self, slot: str):
        super().__init__(slot)
        self.slot = slot
        self.attended = slot in ATTENDED

    @property
    def status(self) -> int:
        """403 for a locked attended slot (the human must approve), 503 for a plain
        unprovisioned one (bootstrap was not run) -- different faults, different fixes."""
        return 403 if self.attended else 503

    @property
    def detail(self) -> str:
        return slot_unavailable_detail(self.slot)


def slot_unavailable_detail(slot: str) -> str:
    """Client-facing explanation for a slot that cannot be served right now."""
    if slot in ATTENDED:
        return (f"attended route: slot '{slot}' is locked. Run "
                f"`security-proxy-unlock {slot}` -- it needs your password or Touch ID, "
                f"and cannot be completed unattended.")
    return f"route not provisioned: slot '{slot}'"


def slot_wipe(slot: str, why: str = "") -> None:
    """Drop an attended slot's value from memory, closing its window."""
    had = SLOTS.pop(slot, None) is not None
    SLOT_STATE.pop(slot, None)
    if had:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - attended slot '{slot}' locked"
              f"{' (' + why + ')' if why else ''}", flush=True)


def slot_arm(slot: str) -> dict | None:
    """Start an attended slot's window on push. Returns the window for the ack."""
    policy = ATTENDED.get(slot)
    if policy is None:
        return None
    ttl = policy.get("ttl")
    uses = policy.get("max_uses")
    SLOT_STATE[slot] = {"expires": (time.monotonic() + ttl) if ttl else None,
                        "uses_left": uses}
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - attended slot '{slot}' unlocked "
          f"(ttl {ttl or 'none'}s, uses {uses or 'unlimited'})", flush=True)
    return {"ttl": ttl, "max_uses": uses}


def slot_get(slot: str | None, consume: bool = True) -> str | None:
    """Read a slot's value, enforcing the attended window.

    Plain slots are a dict lookup. An attended slot is served only inside the window
    opened by `security-proxy-unlock`: past its TTL or its use budget the value is
    wiped, so the next request fails closed and needs a fresh human approval. Pass
    consume=False for reads that are not a request against the credential (rebuilding
    the TLS context), so they do not burn the budget."""
    if not slot:
        return None
    value = SLOTS.get(slot)
    if value is None or slot not in ATTENDED:
        return value
    state = SLOT_STATE.get(slot)
    if state is None:  # armed before the policy existed, or already spent
        slot_wipe(slot, "no window")
        return None
    if state["expires"] is not None and time.monotonic() >= state["expires"]:
        slot_wipe(slot, "ttl expired")
        return None
    if consume and state["uses_left"] is not None:
        state["uses_left"] -= 1
        if state["uses_left"] <= 0:
            # Serve this request, then close the window before the next one.
            slot_wipe(slot, "use budget spent")
    return value


def sweep_attended_slots() -> None:
    """Wipe expired attended slots even if nothing has asked for them.

    slot_get() already fails closed on expiry, but a secret nobody reads would
    otherwise linger in memory past its window -- the point of an attended slot is
    that it is *absent* outside one."""
    now = time.monotonic()
    for slot in list(SLOT_STATE):
        expires = SLOT_STATE[slot]["expires"]
        if expires is not None and now >= expires:
            slot_wipe(slot, "ttl expired")


def resolve_ingest_headers(route: Route) -> dict:
    """Map each of the route's ingest headers to its current slot value.

    Raises SlotUnavailable if a required slot has not been pushed in yet."""
    out = {}
    for header, slot in (route.ingest_headers or {}).items():
        value = slot_get(slot)
        if value is None:
            raise SlotUnavailable(slot)
        out[header] = value
    return out


def build_upstream_headers(request: Request, route: Route) -> dict:
    """Forward safe client headers, then apply ingest-provided headers.

    May raise SlotUnavailable if a required ingest slot is not yet provisioned."""
    inject = resolve_ingest_headers(route)
    drop = {"host", "authorization"}
    if not route.allow_cookies:
        drop.update(REQUEST_COOKIE_HEADERS)
    if route.auth_header:
        drop.add(route.auth_header.lower())
    drop.update(h.lower() for h in inject)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in drop}
    headers.update(inject)
    # Negotiate content-encoding on the client's behalf, not httpx's. We stream the
    # upstream body back raw (aiter_raw), so whatever encoding upstream picks reaches
    # the client verbatim -- and httpx defaults to "gzip, deflate" when the request
    # carries no accept-encoding. A client that sent none (curl, wiff) would then get
    # a gzipped body it never asked for and fail to parse it. Ask for identity instead.
    headers.setdefault("accept-encoding", "identity")
    return headers


def _s3_client_scope(auth: str) -> dict:
    """Parse what a client put in an S3 `Authorization` header.

    SigV4: `AWS4-HMAC-SHA256 Credential=<id>/<date>/<region>/<service>/aws4_request, ...`
    SigV2: `AWS <id>:<signature>` (no region/service). Returns {"access","region",
    "service"}; region/service are None when absent. The access-key-id doubles as the
    gate token (the client signs with a dummy secret; the proxy re-signs for real),
    and the region/service are reused so we sign with the same scope the client used."""
    if auth.startswith("AWS4-HMAC-SHA256"):
        m = re.search(r"Credential=([^/]+)/[^/]+/([^/]+)/([^/]+)/aws4_request", auth)
        if m:
            return {"access": m.group(1), "region": m.group(2), "service": m.group(3)}
        m = re.search(r"Credential=([^/]+)/", auth)
        return {"access": m.group(1) if m else "", "region": None, "service": None}
    if auth.startswith("AWS "):
        return {"access": auth[4:].split(":", 1)[0].strip(), "region": None, "service": None}
    return {"access": "", "region": None, "service": None}


def _s3_canonical_query(query: str) -> str:
    """SigV4 canonical query string: sorted, each key/value URI-encoded."""
    if not query:
        return ""
    pairs = sorted((quote(k, safe="-_.~"), quote(v, safe="-_.~"))
                   for k, v in parse_qsl(query, keep_blank_values=True))
    return "&".join(f"{k}={v}" for k, v in pairs)


def _s3_signing_key(secret: str, datestamp: str, region: str, service: str) -> bytes:
    def _h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()
    k_date = _h(("AWS4" + secret).encode(), datestamp)
    k_region = _h(k_date, region)
    k_service = _h(k_region, service)
    return _h(k_service, "aws4_request")


def sign_s3_v4(method: str, canonical_uri: str, query: str, signed_headers: dict,
               payload_hash: str, region: str, service: str, access_key: str,
               secret_key: str, amz_date: str, datestamp: str) -> str:
    """Compute the AWS SigV4 `Authorization` header value for an S3 request.

    `signed_headers` maps lowercase header name -> value for every header that the
    signature covers (must include host, x-amz-date, x-amz-content-sha256)."""
    names = sorted(signed_headers)
    canonical_headers = "".join(f"{n}:{signed_headers[n].strip()}\n" for n in names)
    signed_list = ";".join(names)
    canonical_request = "\n".join([
        method, canonical_uri, _s3_canonical_query(query),
        canonical_headers, signed_list, payload_hash,
    ])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    key = _s3_signing_key(secret_key, datestamp, region, service)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_list}, Signature={signature}")


def _jwt_exp(token: str) -> int | None:
    """Decode a JWT's `exp` claim without verifying the signature (we only hold it)."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # restore base64 padding
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    exp = claims.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


_B64URL_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


def _looks_like_jwt(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 3 and all(_B64URL_SEGMENT.match(p) for p in parts)


def _extract_token(pasted: str, param: str) -> str:
    """Extract the token from pasted input: a bare JWT, or a URL/query containing `param`."""
    pasted = pasted.strip()
    if _looks_like_jwt(pasted):
        return pasted
    # Treat as a URL or query string and pull out the named parameter
    query = urlsplit(pasted).query or pasted
    values = parse_qs(query).get(param)
    if values and _looks_like_jwt(values[0]):
        return values[0]
    raise ValueError(
        f"could not find a JWT (paste the token itself or a URL containing '{param}=')"
    )


def keychain_get_token(service: str, account: str, keychain: str | None = None) -> str | None:
    """Read a generic-password token from the macOS Keychain (None if absent).

    `-w` takes no argument here (it prints the password to stdout), so only the
    non-secret service/account appear in argv; the secret never does.

    `keychain` names an explicit keychain file rather than searching the default list.
    Attended slots use that to live in a locked, out-of-search-list keychain, so
    reading them raises the macOS unlock prompt instead of succeeding silently."""
    cmd = ["security", "find-generic-password", "-s", service, "-a", account, "-w"]
    if keychain:
        cmd.append(str(Path(keychain).expanduser()))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def keychain_set_token(service: str, account: str, token: str) -> None:
    """Store/replace a generic-password token in the macOS Keychain without argv exposure.

    `security -i` parses the subcommand (including the secret) from stdin, so the
    running `security` process's argv is only `security -i` -- the token never shows
    up in `ps`. Newlines are rejected because `-i` reads line-by-line, so a newline in
    a value would split into (i.e. inject) a second interactive command."""
    for name, value in (("service", service), ("account", account), ("token", token)):
        if "\n" in value or "\r" in value:
            raise ValueError(f"keychain {name} must not contain newlines")
    command = "add-generic-password -U -D {desc} -s {svc} -a {acct} -w {tok}\n".format(
        desc=shlex.quote("security-proxy SSO token"),
        svc=shlex.quote(service), acct=shlex.quote(account), tok=shlex.quote(token),
    )
    result = subprocess.run(["security", "-i"], input=command,
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"keychain write failed: {result.stderr.strip()}")


def _file_get_token(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text()).get("token") or None
    except (ValueError, OSError):
        return None


def _file_set_token(path: Path, token: str, upstream: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps({"token": token, "upstream": upstream}))
    path.chmod(0o600)


def _sso_storage(sso: dict, route: Route):
    """Return (load, save, description) for the route's configured token backend."""
    if sso.get("keychain"):
        service = sso["keychain"] if isinstance(sso["keychain"], str) else "security-proxy"
        account = sso.get("keychain_account") or urlparse(route.upstream).netloc
        descr = f'macOS Keychain (service "{service}", account "{account}")'
        return (lambda: keychain_get_token(service, account),
                lambda tok: keychain_set_token(service, account, tok),
                descr)
    if sso.get("token_cache"):
        path = Path(sso["token_cache"]).expanduser()
        return (lambda: _file_get_token(path),
                lambda tok: _file_set_token(path, tok, route.upstream),
                str(path))
    return (lambda: None, lambda tok: None, "(no persistence configured)")


def acquire_sso_token(route: Route) -> str:
    """Obtain the upstream's SSO token, from storage if valid, else via interactive login."""
    sso = route.sso
    param = sso.get("param", "token")
    load, save, descr = _sso_storage(sso, route)

    # 1. Try stored token
    cached = load()
    if cached:
        exp = _jwt_exp(cached)
        if exp is None or exp > time.time():
            _log_expiry(route.prefix, exp)
            return cached
        print(f"[{route.prefix}] stored SSO token expired; re-login required")

    # 2. Interactive login (requires a terminal / stdin)
    login_url = sso.get("login_url") or route.upstream
    if not os.isatty(0):
        raise RuntimeError(
            f"[{route.prefix}] no valid SSO token and no terminal to log in. "
            f"Run the proxy once in a terminal to seed {descr}."
        )
    print(f"\n[{route.prefix}] CERN SSO login required for {route.upstream}")
    print(f"  Opening {login_url}")
    print("  After logging in, copy the 'token' value from the address bar")
    print("  (or paste the whole redirected URL) and paste it below.\n")
    try:
        webbrowser.open(login_url)
    except Exception:
        pass

    while True:
        pasted = input(f"[{route.prefix}] token (or URL): ")
        try:
            token = _extract_token(pasted, param)
            break
        except ValueError as e:
            print(f"  {e}")

    # 3. Persist for unattended restarts
    save(token)
    print(f"  Saved to {descr}")
    _log_expiry(route.prefix, _jwt_exp(token))
    return token


def _log_expiry(prefix: str, exp: int | None) -> None:
    if exp is None:
        print(f"[{prefix}] SSO token loaded (no expiry claim)")
        return
    remaining = exp - time.time()
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(exp))
    msg = f"[{prefix}] SSO token valid until {when}"
    if remaining < 7 * 86400:
        msg += f"  WARNING: expires in {remaining / 86400:.1f} days"
    print(msg)


_KEYCHAIN_IDENTITY_RE = re.compile(r'^\s*\d+\)\s+([0-9A-Fa-f]{40})\s+"(.*)"\s*$')


def parse_keychain_identities(output: str) -> list[KeychainIdentity]:
    identities = []
    for line in output.splitlines():
        match = _KEYCHAIN_IDENTITY_RE.match(line)
        if match:
            identities.append(KeychainIdentity(match.group(1).upper(), match.group(2)))
    return identities


def list_keychain_identities() -> list[KeychainIdentity]:
    result = subprocess.run(
        ["security", "find-identity", "-v"],
        check=True, capture_output=True, text=True,
    )
    return parse_keychain_identities(result.stdout)


def resolve_keychain_identity(identity: str,
                              identities: list[KeychainIdentity] | None = None) -> KeychainIdentity:
    """Resolve a user-provided identity name or SHA-1 fingerprint to exactly one identity."""
    identities = identities if identities is not None else list_keychain_identities()
    wanted = identity.strip()
    wanted_sha1 = wanted.replace(":", "").upper()
    if re.fullmatch(r"[0-9A-F]{40}", wanted_sha1):
        matches = [item for item in identities if item.sha1 == wanted_sha1]
    else:
        matches = [item for item in identities if item.name == wanted]
        if not matches:
            lowered = wanted.lower()
            matches = [item for item in identities if lowered in item.name.lower()]

    if not matches:
        raise RuntimeError(f"no Keychain identity matches {identity!r}")
    unique = {(item.sha1, item.name) for item in matches}
    if len(unique) != 1:
        names = ", ".join(f"{item.sha1} {item.name!r}" for item in matches)
        raise RuntimeError(f"ambiguous Keychain identity {identity!r}: {names}")
    return matches[0]


_PEM_BLOCK_RE = re.compile(
    rb"-----BEGIN ([A-Z ]+)-----.*?-----END \1-----",
    re.DOTALL,
)


def _public_key_der(public_key) -> bytes:
    import cryptography.hazmat.primitives.serialization as ser

    return public_key.public_bytes(
        ser.Encoding.DER,
        ser.PublicFormat.SubjectPublicKeyInfo,
    )


def _load_identity_export(pem_data: bytes):
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    certs = []
    keys = []
    for match in _PEM_BLOCK_RE.finditer(pem_data):
        block = match.group(0)
        label = match.group(1)
        if label == b"CERTIFICATE":
            certs.append(x509.load_pem_x509_certificate(block))
        elif label.endswith(b"PRIVATE KEY"):
            keys.append(load_pem_private_key(block, password=None))
    return certs, keys


def export_keychain_identity(identity: str, output_p12: Path, password: str) -> None:
    """Export exactly one certificate + key from the macOS Keychain to a PKCS12 file."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.serialization import BestAvailableEncryption
    from cryptography.hazmat.primitives.serialization.pkcs12 import serialize_key_and_certificates

    selected = resolve_keychain_identity(identity)
    export_password = os.urandom(16).hex()
    tmp_dir = Path(tempfile.mkdtemp(prefix="security-proxy-keychain-"))
    tmp_dir.chmod(0o700)
    all_p12 = tmp_dir / "all-identities.p12"
    try:
        subprocess.run(
            ["security", "export", "-t", "identities", "-f", "pkcs12",
             "-P", export_password, "-o", str(all_p12)],
            check=True, capture_output=True,
        )
        all_p12.chmod(0o600)
        unpacked = subprocess.run(
            ["/usr/bin/openssl", "pkcs12", "-in", str(all_p12), "-passin", f"pass:{export_password}",
             "-nodes"],
            check=True, capture_output=True,
        ).stdout

        certs, keys = _load_identity_export(unpacked)
        selected_cert = None
        for cert in certs:
            if cert.fingerprint(hashes.SHA1()).hex().upper() == selected.sha1:
                selected_cert = cert
                break
        if selected_cert is None:
            raise RuntimeError(f"Keychain export did not contain identity {selected.sha1} {selected.name!r}")

        selected_pub = _public_key_der(selected_cert.public_key())
        matching_keys = [key for key in keys if _public_key_der(key.public_key()) == selected_pub]
        if len(matching_keys) != 1:
            raise RuntimeError(
                f"Keychain export contained {len(matching_keys)} private keys for "
                f"identity {selected.sha1} {selected.name!r}"
            )

        identity_cert_pubs = {_public_key_der(key.public_key()) for key in keys}
        chain = [
            cert for cert in certs
            if cert is not selected_cert and _public_key_der(cert.public_key()) not in identity_cert_pubs
        ]
        output_p12.write_bytes(serialize_key_and_certificates(
            name=selected.name.encode(),
            key=matching_keys[0],
            cert=selected_cert,
            cas=chain,
            encryption_algorithm=BestAvailableEncryption(password.encode()),
        ))
        output_p12.chmod(0o600)
    finally:
        all_p12.unlink(missing_ok=True)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


def build_ssl_context(args) -> ssl.SSLContext:
    """Build an SSL context with client certificate from the configured source."""
    ssl_ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)

    if args.cafile:
        ssl_ctx.load_verify_locations(cafile=args.cafile)

    if CERT_SLOT:
        # Building the TLS context is not a request against the credential, so it must
        # not burn an attended slot's use budget.
        cert_pem = slot_get(CERT_SLOT, consume=False)
        if cert_pem is None:
            raise SlotUnavailable(CERT_SLOT)
        key_pem = slot_get(KEY_SLOT, consume=False) if KEY_SLOT else None
        if KEY_SLOT and key_pem is None:
            raise SlotUnavailable(KEY_SLOT)
        _load_pem_into_context(ssl_ctx, cert_pem, key_pem)
    elif args.p12:
        p12_path = Path(args.p12).expanduser()
        p12_password = (args.p12_password or "").encode()
        _load_p12_into_context(ssl_ctx, p12_path, p12_password)
    elif args.keychain_identity:
        p12_password = os.urandom(16).hex()
        p12_path = Path.home() / ".globus" / "security-proxy-keychain.p12"
        export_keychain_identity(args.keychain_identity, p12_path, p12_password)
        _load_p12_into_context(ssl_ctx, p12_path, p12_password.encode())
        p12_path.unlink()
    else:
        ssl_ctx.load_cert_chain(certfile=args.cert, keyfile=args.key)

    return ssl_ctx


def _load_p12_into_context(ssl_ctx: ssl.SSLContext, p12_path: Path, password: bytes) -> None:
    """Load a PKCS12 file into an SSL context by converting to temp PEM files."""
    import cryptography.hazmat.primitives.serialization as ser
    from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates

    p12_data = p12_path.read_bytes()
    private_key, certificate, chain = load_key_and_certificates(p12_data, password or None)

    tmp_dir = Path(tempfile.mkdtemp(prefix="security-proxy-"))
    cert_pem = tmp_dir / "cert.pem"
    key_pem = tmp_dir / "key.pem"

    cert_bytes = certificate.public_bytes(ser.Encoding.PEM)
    if chain:
        for ca_cert in chain:
            cert_bytes += ca_cert.public_bytes(ser.Encoding.PEM)
    cert_pem.write_bytes(cert_bytes)
    cert_pem.chmod(0o600)

    key_bytes = private_key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption())
    key_pem.write_bytes(key_bytes)
    key_pem.chmod(0o600)

    ssl_ctx.load_cert_chain(certfile=str(cert_pem), keyfile=str(key_pem))

    key_pem.unlink()
    cert_pem.unlink()
    tmp_dir.rmdir()


def _load_pem_into_context(ssl_ctx: ssl.SSLContext, cert_pem: str, key_pem: str | None) -> None:
    """Load an in-memory PEM cert (+ optional key) by briefly materialising 0600 temp
    files -- stdlib ssl.load_cert_chain only reads from files. The key never persists.
    If key_pem is None, the cert PEM must also contain the private key."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="security-proxy-"))
    cert_file = tmp_dir / "cert.pem"
    cert_file.write_text(cert_pem)
    cert_file.chmod(0o600)
    key_file = None
    if key_pem:
        key_file = tmp_dir / "key.pem"
        key_file.write_text(key_pem)
        key_file.chmod(0o600)
    try:
        ssl_ctx.load_cert_chain(certfile=str(cert_file),
                                keyfile=str(key_file) if key_file else None)
    finally:
        cert_file.unlink(missing_ok=True)
        if key_file:
            key_file.unlink(missing_ok=True)
        tmp_dir.rmdir()


async def mint_alien_token(source_ctx: ssl.SSLContext) -> tuple[str, str]:
    """Obtain a short-lived JAliEn token certificate + key, authenticating with the
    grid/host certificate in `source_ctx`.

    This is what `alien.py token` / `alien-token-init` does, without the alienpy
    dependency: open a WebSocket to JAliEn central authenticated by the client
    certificate, send the `token` command, and read the returned PEMs back out.
    Protocol (alienpy: wb_api.py `token()` / tools_nowb.py `CreateJsonCommand`):
        -> {"command": "token", "options": ["-nomsg"]}
        <- {"results": [{"tokencert": "<PEM>", "tokenkey": "<PEM>"}]}
    Returns (tokencert_pem, tokenkey_pem)."""
    endpoint = (ALIEN_TOKEN or {}).get("endpoint") or DEFAULT_ALIEN_ENDPOINT
    request = json.dumps({"command": "token", "options": ["-nomsg"]})
    async with websockets.connect(endpoint, ssl=source_ctx) as ws:
        await ws.send(request)
        reply = json.loads(await ws.recv())
    results = reply.get("results") or []
    if not results:
        raise RuntimeError(f"JAliEn token request returned no results: {reply.get('metadata', reply)}")
    cert_pem = results[0].get("tokencert") or ""
    key_pem = results[0].get("tokenkey") or ""
    if not cert_pem or not key_pem:
        raise RuntimeError("JAliEn token request succeeded but tokencert/tokenkey were empty")
    return cert_pem, key_pem


async def rebuild_tls() -> bool:
    """(Re)build the SSL context + httpx client from the current cert source.

    Returns False (leaving any existing client in place) if the cert is supplied via
    an ingest slot that has not been provisioned yet.

    With `alien_token` configured, the configured certificate is used only to mint a
    short-lived JAliEn token, and the *token* becomes the client certificate for
    upstream mTLS -- so the long-lived grid/host cert is never presented upstream."""
    global ssl_ctx, http_client
    try:
        new_ctx = build_ssl_context(APP_ARGS)
    except SlotUnavailable:
        return False
    if ALIEN_TOKEN:
        try:
            cert_pem, key_pem = await mint_alien_token(new_ctx)
        except Exception as exc:
            # Keep any existing (still-valid) token in place rather than dropping to
            # an unauthenticated context.
            print(f"alien token: could not mint: {exc}", flush=True)
            return False
        token_ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        if APP_ARGS.cafile:
            token_ctx.load_verify_locations(cafile=APP_ARGS.cafile)
        _load_pem_into_context(token_ctx, cert_pem, key_pem)
        new_ctx = token_ctx
        print(f"{time.strftime('%Y-%m-%d %H:%M')} - minted JAliEn token certificate", flush=True)
    old = http_client
    ssl_ctx = new_ctx
    http_client = httpx.AsyncClient(verify=new_ctx, timeout=300.0)
    if old is not None:
        await old.aclose()
    return True


def inject_query_token(url: str, param: str, token: str) -> str:
    """Return `url` with `param=token` set in its query, dropping any existing `param`."""
    parts = urlsplit(url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    pairs.append((param, token))
    return urlunsplit(parts._replace(query=urlencode(pairs)))


def rewrite_url(value: str, upstream: str, proxy_base: str, prefix: str) -> str:
    """Rewrite URLs pointing to the upstream server to point to the proxy instead."""
    if value.startswith(upstream):
        return proxy_base + "/" + prefix + value[len(upstream):]
    return value


@asynccontextmanager
async def lifespan(app):
    global APP_ARGS
    APP_ARGS = app.state.args
    await rebuild_tls()  # builds now if file-based (or cert slot already set); else
    #                      stays unprovisioned until the cert is pushed via ingest
    yield
    if http_client:
        await http_client.aclose()


app = FastAPI(lifespan=lifespan)


@app.websocket("/{path:path}")
async def ws_proxy(ws: WebSocket, path: str):
    route = match_route(path, ws.headers)
    if route is None:
        raise WebSocketDisconnect(code=4004, reason="No route matches this path")
    requested_subprotocols = parse_ws_subprotocols(ws.headers.get("sec-websocket-protocol", ""))
    subprotocol_token, subprotocols = split_ws_gate_subprotocols(requested_subprotocols)

    # Gate token: route.auth_header, else Authorization, else ?token=, else the reserved
    # `security-proxy-token.<token>` subprotocol, which is stripped before forwarding.
    fallback = ws.query_params.get("token", "") or subprotocol_token
    presented = presented_token(ws.headers, route, fallback)
    if not gate_ok(presented, route.name):
        legacy_token = ""
        if not route.auth_header and not presented and not fallback:
            for subprotocol in subprotocols:
                if gate_ok(subprotocol, route.name):
                    legacy_token = subprotocol
                    break
        if legacy_token:
            subprotocols = [s for s in subprotocols if s != legacy_token]
        else:
            raise WebSocketDisconnect(code=4001, reason="Invalid token")
    if not route.websocket:
        raise WebSocketDisconnect(code=4000, reason="Route does not support WebSocket")
    if ssl_ctx is None:
        raise WebSocketDisconnect(code=4003, reason="TLS certificate not provisioned")

    # Resolve ingest-provided upstream headers up front; reject if not provisioned
    try:
        ingest_headers = resolve_ingest_headers(route)
    except SlotUnavailable as e:
        raise WebSocketDisconnect(code=4003, reason=e.detail[:120])

    # Header-matched routes forward the full path; prefix routes strip their prefix
    upstream_path = path if route.auth_header else path[len(route.prefix.lstrip("/")):]
    upstream_path = upstream_path.lstrip("/")
    try:
        upstream_url = upstream_url_for(route, upstream_path)
    except PathTraversal:
        raise WebSocketDisconnect(code=4005, reason="path escapes the route's prefix")
    # Forward query params, but drop the proxy's own auth "token" so it never leaks upstream
    fwd = [(k, v) for k, v in parse_qsl(ws.url.query, keep_blank_values=True) if k != "token"]
    if fwd:
        sep = "&" if "?" in upstream_url else "?"
        upstream_url = f"{upstream_url}{sep}{urlencode(fwd)}"

    # Inject the captured SSO credential as a query param (web-ui WS auth reads ?token=)
    if route.injected_token and (route.sso or {}).get("inject", "query") != "bearer":
        upstream_url = inject_query_token(upstream_url, (route.sso or {}).get("param", "token"),
                                          route.injected_token)

    await ws.accept(subprotocol=subprotocols[0] if subprotocols else None)

    connect_kwargs = {"ssl": ssl_ctx}
    if subprotocols:
        connect_kwargs["subprotocols"] = subprotocols
    if ingest_headers:
        connect_kwargs["additional_headers"] = ingest_headers

    async with websockets.connect(upstream_url, **connect_kwargs) as upstream_ws:
        async def client_to_upstream():
            try:
                while True:
                    data = await ws.receive_text()
                    await upstream_ws.send(data)
            except WebSocketDisconnect:
                await upstream_ws.close()

        async def upstream_to_client():
            try:
                async for msg in upstream_ws:
                    if isinstance(msg, str):
                        await ws.send_text(msg)
                    else:
                        await ws.send_bytes(msg)
            except websockets.exceptions.ConnectionClosed:
                await ws.close()

        await asyncio.gather(client_to_upstream(), upstream_to_client())


async def proxy_s3(path: str, request: Request, route: Route) -> Response:
    """Forward an S3 request, signing it with the real keys (SigV4).

    The client (e.g. s3cmd) sends an *unsigned-in-effect* request: its access-key-id
    is the proxy gate token and it signs with a dummy secret. We drop that signature
    and recompute SigV4 against the real upstream host with the real access/secret
    keys (held only in memory, pushed in via ingest). Payload is signed as
    UNSIGNED-PAYLOAD so large object uploads need not be hashed (the upstream hop is
    HTTPS), and path-style addressing is assumed (bucket is the first path segment)."""
    s3 = route.s3_sign
    # Path-style addressing: the first path segment is the bucket. Pick its keypair,
    # falling back to the route's default keypair for buckets without their own.
    bucket = path.split("/", 1)[0]
    keypair = s3["buckets"].get(bucket) or s3["default"]
    if keypair is None:
        raise HTTPException(status_code=404,
                            detail=f"no S3 credentials configured for bucket '{bucket}'")
    # Both halves are one credential, so both spend a use and expire together.
    access_key = slot_get(keypair["access_slot"])
    secret_key = slot_get(keypair["secret_slot"])
    for role, value in (("access_slot", access_key), ("secret_slot", secret_key)):
        if value is None:
            exc = SlotUnavailable(keypair[role])
            raise HTTPException(status_code=exc.status, detail=exc.detail)

    upstream = urlsplit(route.upstream)
    host = upstream.netloc
    # Sign with the region/service the client signed with (SigV4 always sends a region);
    # fall back to the configured override only if the client sent none (e.g. SigV2).
    scope = _s3_client_scope(request.headers.get("authorization", ""))
    region = scope["region"] or s3["region"]
    service = scope["service"] or s3["service"]
    if not region:
        raise HTTPException(status_code=400,
                            detail="no S3 signing region: client sent none (SigV2?); "
                                   "use signature_v2=False or set s3.region in the route")
    # Use the raw (still percent-encoded) path so the signature matches the wire bytes.
    raw = request.scope.get("raw_path")
    canonical_uri = raw.decode("latin-1") if raw else "/" + quote(path, safe="/-_.~")
    query = request.url.query
    body = await request.body()

    amz_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    datestamp = amz_date[:8]
    payload_hash = "UNSIGNED-PAYLOAD"

    # Forward the client's headers except the ones we set/replace ourselves.
    drop = {"host", "authorization", "content-length", "transfer-encoding",
            "connection", "keep-alive", "x-amz-date", "x-amz-content-sha256"}
    if not route.allow_cookies:
        drop.update(REQUEST_COOKIE_HEADERS)
    fwd = {k: v for k, v in request.headers.items() if k.lower() not in drop}

    # Signature must cover host, our x-amz-* headers, and any x-amz-* the client sent
    # (S3 requires all x-amz-* headers to be signed).
    signed = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    for k, v in fwd.items():
        if k.lower().startswith("x-amz-"):
            signed[k.lower()] = v
    auth = sign_s3_v4(request.method, canonical_uri, query, signed, payload_hash,
                      region, service, access_key, secret_key, amz_date, datestamp)

    out_headers = dict(fwd)  # host is set by httpx from the URL; matches signed host
    out_headers["x-amz-date"] = amz_date
    out_headers["x-amz-content-sha256"] = payload_hash
    out_headers["authorization"] = auth

    url = f"{upstream.scheme}://{host}{canonical_uri}"
    if query:
        url = f"{url}?{query}"

    req = http_client.build_request(method=request.method, url=url,
                                    headers=out_headers, content=body)
    resp = await http_client.send(req, stream=True, follow_redirects=False)

    excluded = {"transfer-encoding", "connection", "keep-alive"}
    if not route.allow_cookies:
        excluded.update(RESPONSE_COOKIE_HEADERS)
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}

    async def stream_body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(stream_body(), status_code=resp.status_code, headers=resp_headers)


def _oauth_current_refresh(account: dict) -> str | None:
    """The account's current refresh token: the persisted (rotated) one if a
    `refresh_store` is configured and populated, otherwise the ingest-provisioned
    slot. Providers that rotate the refresh token on each use (Microsoft) leave the
    Keychain-seeded slot stale after the first refresh, so a live store wins."""
    store = account.get("refresh_store")
    if store:
        persisted = _file_get_token(Path(store).expanduser())
        if persisted:
            return persisted
    return slot_get(account["refresh_slot"])


async def proxy_oauth(path: str, request: Request, route: Route) -> Response:
    """Broker an OAuth2 refresh-token grant, returning only the short-lived access token.

    mbsync/oama-style clients authenticate to IMAP over XOAUTH2 with a ~1h access
    token, but the durable secrets -- the OAuth client_secret and the long-lived
    refresh_token -- must never live on the client. This route holds them (in memory,
    pushed in via ingest) and performs the refresh: the client GETs `/<route>/<account>`
    with the route's gate token, we POST the refresh grant to the provider's token
    endpoint and return just what the provider replied *minus* any rotated
    refresh_token, so the client only ever sees the access token (+ its expiry).

    A rotated refresh_token in the reply is captured (updated in memory, and persisted
    to `refresh_store` if configured) so subsequent refreshes -- and restarts -- keep
    working without the client ever holding a long-lived credential."""
    oauth = route.oauth
    account_name = path[len(route.prefix.lstrip("/")):].strip("/")
    account = oauth["accounts"].get(account_name)
    if account is None:
        raise HTTPException(status_code=404,
                            detail=f"no OAuth account '{account_name}' on route '{route.name}'")
    # A confidential client has a client_secret slot; a public client (e.g. the
    # Microsoft device-code flow) has none, and the refresh POST omits it.
    client_secret = None
    if account["secret_slot"]:
        client_secret = slot_get(account["secret_slot"])
        if client_secret is None:
            exc = SlotUnavailable(account["secret_slot"])
            raise HTTPException(status_code=exc.status, detail=exc.detail)
    refresh_token = _oauth_current_refresh(account)
    if not refresh_token:
        exc = SlotUnavailable(account["refresh_slot"])
        raise HTTPException(status_code=exc.status, detail=exc.detail)

    form = {
        "grant_type": "refresh_token",
        "client_id": account["client_id"],
        "refresh_token": refresh_token,
    }
    if client_secret is not None:
        form["client_secret"] = client_secret
    if account.get("scope"):  # Microsoft needs the IMAP scope on refresh; Google ignores it
        form["scope"] = account["scope"]

    resp = await http_client.post(
        account["endpoint"], data=form,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(status_code=502,
                            detail=f"token endpoint returned non-JSON (status {resp.status_code})")

    # Capture a rotated refresh token so the next refresh (and, with a store, the next
    # restart) uses the fresh one -- the client never gets to see it.
    new_rt = data.get("refresh_token")
    if isinstance(new_rt, str) and new_rt and new_rt != refresh_token:
        SLOTS[account["refresh_slot"]] = new_rt
        store = account.get("refresh_store")
        if store:
            _file_set_token(Path(store).expanduser(), new_rt, account["endpoint"])
    data.pop("refresh_token", None)  # only the access token leaves the proxy

    return JSONResponse(data, status_code=resp.status_code)


def _sign_load_key(route: Route):
    """Load the route's Ed25519 signing key from its ingest slot, returning
    (private_key, raw_pubkey_bytes, keyid). Raises HTTPException(503) if the slot is
    unprovisioned, 500 if the stored value is not a valid 32-byte seed.

    The slot holds the raw 32-byte Ed25519 seed base64-encoded (ingest slot values are
    strings). keyid = sha256(raw pubkey), matching alibuild_helpers.signing.keyid_for."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    seed_b64 = slot_get(route.sign["key_slot"])
    if seed_b64 is None:
        exc = SlotUnavailable(route.sign["key_slot"])
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    try:
        seed = base64.b64decode(seed_b64, validate=True)
    except (ValueError, base64.binascii.Error):
        raise HTTPException(status_code=500, detail="sign key slot is not valid base64")
    if len(seed) != 32:
        raise HTTPException(status_code=500,
                            detail=f"sign key must be a 32-byte Ed25519 seed, got {len(seed)}")
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    pub = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private_key, pub, hashlib.sha256(pub).hexdigest()


async def proxy_sign(path: str, request: Request, route: Route) -> Response:
    """Local Ed25519 signer for alibuild AC entries (spec: ALIBUILD_SIGN_ROUTE.md).

    Two endpoints under the route prefix:
      * POST <prefix>            -- sign the raw request body (the client-built DSSE PAE)
                                    with the proxy-held key. Returns {"keyid", "sig"}.
      * GET  <prefix>/pubkey     -- publish the raw public key (non-secret), so a keyring
                                    can be built. Returns {"keyid", "publicKey"}.
    A "dumb signer": it signs exactly the bytes it receives (Ed25519 is deterministic,
    so the signature is byte-for-byte identical to a local sign() over the same PAE) and
    never parses, wraps, or re-encodes them. The gate token is the authorization
    boundary; binding a signature to the actual artifact is the client's/verifier's job,
    not the proxy's."""
    private_key, pub, keyid = _sign_load_key(route)
    subpath = path[len(route.prefix.strip("/")):].lstrip("/")

    if subpath == "pubkey" and request.method == "GET":
        return JSONResponse({"keyid": keyid,
                             "publicKey": base64.b64encode(pub).decode("ascii")})
    if subpath == "" and request.method == "POST":
        body = await request.body()  # opaque bytes; signed exactly as received
        return JSONResponse({"keyid": keyid,
                             "sig": base64.b64encode(private_key.sign(body)).decode("ascii")})
    raise HTTPException(status_code=404, detail="unknown sign endpoint")


@app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request):
    route = match_route(path, request.headers)
    if route is None:
        raise HTTPException(status_code=404, detail="No route matches this path")
    # S3 routes carry the gate token as the access-key-id in the Authorization header
    presented = (_s3_client_scope(request.headers.get("authorization", ""))["access"]
                 if route.s3_sign else presented_token(request.headers, route))
    if not gate_ok(presented, route.name):
        raise HTTPException(status_code=401, detail="Invalid token")
    # Local signing terminates here -- no upstream, so it needs no TLS client cert.
    if route.sign:
        return await proxy_sign(path, request, route)
    if http_client is None:
        raise HTTPException(status_code=503, detail="TLS certificate not provisioned")
    if route.s3_sign:
        return await proxy_s3(path, request, route)
    if route.oauth:
        return await proxy_oauth(path, request, route)

    # Header-matched routes forward the full path; prefix routes strip their prefix
    upstream_path = path if route.auth_header else path[len(route.prefix.lstrip("/")):]
    upstream_path = upstream_path.lstrip("/")

    # Forward headers (minus host/auth), applying ingest-provided upstream headers
    try:
        headers = build_upstream_headers(request, route)
    except SlotUnavailable as e:
        raise HTTPException(status_code=e.status, detail=e.detail)

    body = await request.body()
    try:
        url = upstream_url_for(route, upstream_path)
    except PathTraversal:
        raise HTTPException(status_code=400, detail="path escapes the route's prefix")
    if request.url.query:
        url = f"{url}?{request.url.query}"

    # Inject the captured SSO credential for SSO routes
    if route.injected_token:
        if (route.sso or {}).get("inject", "query") == "bearer":
            headers["authorization"] = f"Bearer {route.injected_token}"
        else:
            url = inject_query_token(url, (route.sso or {}).get("param", "token"),
                                     route.injected_token)

    # Stream the upstream response instead of buffering the whole body in memory.
    # Buffering (resp.content) meant large files -- e.g. multi-100 MB perf
    # profiles -- never emitted a first byte before the client timed out, while
    # small files and Range requests buffered fast enough to slip through.
    req = http_client.build_request(
        method=request.method,
        url=url,
        headers=headers,
        content=body,
    )
    resp = await http_client.send(req, stream=True, follow_redirects=False)

    proxy_base = f"{request.url.scheme}://{request.headers['host']}"
    prefix = route.prefix.strip("/")

    # Forward response headers, excluding hop-by-hop and rewriting locations
    excluded = {"transfer-encoding", "connection", "keep-alive"}
    if not route.allow_cookies:
        excluded.update(RESPONSE_COOKIE_HEADERS)
    rewrite_headers = {"content-location", "location"}
    resp_headers = {}
    for k, v in resp.headers.items():
        if k.lower() in excluded:
            continue
        if k.lower() in rewrite_headers:
            resp_headers[k] = ", ".join(
                rewrite_url(part.strip(), route.upstream, proxy_base, prefix)
                for part in v.split(",")
            )
        else:
            resp_headers[k] = v

    # aiter_raw() forwards the bytes exactly as received, so the forwarded
    # content-encoding / content-length stay consistent (no gzip-length
    # mismatch -- clients no longer need "Accept-Encoding: identity").
    async def stream_body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=resp.status_code,
        headers=resp_headers,
    )


def _socket_modes(socket_gid: int | None) -> tuple[int, int]:
    """(socket mode, dir mode) -- group-accessible when a socket group is configured."""
    return (0o660, 0o2750) if socket_gid is not None else (0o600, 0o700)


def _finalize_socket(socket_path: Path, socket_gid: int | None) -> None:
    """Apply the chosen mode (and group) to a freshly bound socket."""
    os.chmod(socket_path, _socket_modes(socket_gid)[0])
    if socket_gid is not None:
        try:
            os.chown(socket_path, -1, socket_gid)
        except OSError as exc:
            if socket_path.stat().st_gid != socket_gid:
                raise RuntimeError(f"cannot set group on socket {socket_path}") from exc


def _prepare_socket_dir(socket_path: Path, socket_gid: int | None) -> None:
    """Ensure the socket's parent dir exists with the right perms; clear stale socket."""
    _, dir_mode = _socket_modes(socket_gid)
    parent = socket_path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=dir_mode)
    try:  # tighten only a dir we own (skip shared dirs like /tmp)
        if parent.stat().st_uid == os.getuid():
            # chown BEFORE chmod: for a non-root caller chown() clears the setgid bit,
            # so doing it after chmod would strip the S_ISGID we just set. The setgid
            # bit is what makes the socket inherit the dir's group on Linux (on BSD the
            # group is inherited from the parent regardless, so this only showed up as
            # the dir drifting 2750 -> 0750 on every restart).
            if socket_gid is not None:
                try:
                    os.chown(parent, -1, socket_gid)
                except OSError as exc:
                    if parent.stat().st_gid != socket_gid:
                        raise RuntimeError(f"cannot set group on socket directory {parent}") from exc
            os.chmod(parent, dir_mode)
    except OSError as exc:
        raise RuntimeError(f"cannot prepare socket directory {parent}") from exc
    socket_path.unlink(missing_ok=True)


async def run_ingest(socket_path: Path, socket_gid: int | None = None):
    """Write-only ingest socket: accept {"slot","value"} pushes, store them in memory.

    Never returns a stored secret -- it replies only with an ack -- so it cannot be
    used to read secrets back out, only to set them. Restricted by 0600 perms."""
    _prepare_socket_dir(socket_path, socket_gid)

    async def handle(reader, writer):
        try:
            msg = json.loads((await reader.readline()).decode() or "{}")
            slot, value = msg.get("slot"), msg.get("value")
            if isinstance(slot, str) and slot and msg.get("clear") is True:
                # `security-proxy-unlock --lock`: close an attended window early. Anyone
                # who can reach this socket can already overwrite a slot, so allowing an
                # explicit wipe adds no exposure and makes locking up cheap.
                slot_wipe(slot, "locked on request")
                resp = {"ok": True, "slot": slot, "cleared": True}
            elif isinstance(slot, str) and slot and isinstance(value, str):
                SLOTS[slot] = value
                resp = {"ok": True, "slot": slot}
                window = slot_arm(slot)
                if window:
                    resp["attended"] = window
                if slot in (CERT_SLOT, KEY_SLOT):  # (re)build TLS when the cert arrives
                    try:
                        resp["tls"] = "ready" if await rebuild_tls() else "incomplete"
                    except Exception as exc:
                        resp["tls"] = f"error: {exc}"
            else:
                resp = {"ok": False, "error": 'expected {"slot": str, "value": str}'}
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    _finalize_socket(socket_path, socket_gid)
    return server


async def run_agent(socket_path: Path, socket_gid: int | None = None):
    """Serve {port, per-service token} over a 0600 per-user UNIX socket."""
    _prepare_socket_dir(socket_path, socket_gid)

    known = {r.name for r in ROUTES}

    async def handle(reader, writer):
        try:
            service = (await reader.readline()).decode().strip()
            if not service:
                resp = {"port": PROXY_PORT}
            elif service in known:
                resp = {"port": PROXY_PORT, "service": service, "token": service_token(service)}
            else:
                resp = {"error": f"unknown service {service!r}", "services": sorted(known)}
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    _finalize_socket(socket_path, socket_gid)
    return server


async def serve(args, log_config, agent_path: Path, ingest_path: Path, rotation: int):
    """Bind a random localhost port, start the agent + ingest + rotation, run uvicorn."""
    global PROXY_PORT
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, 0))
    PROXY_PORT = sock.getsockname()[1]

    agent = await run_agent(agent_path, AGENT_SOCKET_GID)
    ingest = await run_ingest(ingest_path, INGEST_SOCKET_GID)

    def _cleanup_sockets():
        agent_path.unlink(missing_ok=True)
        ingest_path.unlink(missing_ok=True)
    atexit.register(_cleanup_sockets)  # backstop cleanup

    print(f"Proxy on http://{args.host}:{PROXY_PORT} (random port)", flush=True)
    print(f"  agent socket:  {agent_path}   (security-proxy-token <service>)", flush=True)
    print(f"  ingest socket: {ingest_path}   (security-proxy-bootstrap)", flush=True)
    slots = {s for r in ROUTES for s in (r.ingest_headers or {}).values()}
    for r in ROUTES:
        if r.s3_sign:
            keypairs = list(r.s3_sign["buckets"].values())
            if r.s3_sign["default"]:
                keypairs.append(r.s3_sign["default"])
            slots |= {kp[k] for kp in keypairs for k in ("access_slot", "secret_slot")}
        if r.oauth:
            slots |= {a[k] for a in r.oauth["accounts"].values()
                      for k in ("secret_slot", "refresh_slot") if a[k]}
        if r.sign:
            slots.add(r.sign["key_slot"])
    slots |= {s for s in (CERT_SLOT, KEY_SLOT) if s}
    if slots - set(ATTENDED):
        print(f"  secret slots awaiting provisioning: {sorted(slots - set(ATTENDED))}", flush=True)
    for slot in sorted(slots & set(ATTENDED)):
        pol = ATTENDED[slot]
        print(f"  attended slot (locked until `security-proxy-unlock {slot}`): "
              f"ttl {pol.get('ttl') or 'none'}s, uses {pol.get('max_uses') or 'unlimited'}",
              flush=True)
    for slot in sorted(set(ATTENDED) - slots):
        print(f"  warning: attended_slots lists '{slot}', which no route uses", flush=True)

    async def rotate_loop():
        while True:
            await asyncio.sleep(rotation)
            rotate_master()
            print(f"{time.strftime('%Y-%m-%d %H:%M')} - rotated gate secret", flush=True)

    rot = asyncio.create_task(rotate_loop())

    # Expire attended windows on a timer, not just on access, so an unlocked secret
    # nobody happened to use still leaves memory when its window closes.
    async def attended_sweep_loop():
        while True:
            await asyncio.sleep(ATTENDED_SWEEP_SECONDS)
            sweep_attended_slots()

    sweeper = asyncio.create_task(attended_sweep_loop()) if ATTENDED else None

    # JAliEn tokens are short-lived, so re-mint well before expiry. rebuild_tls() keeps
    # the previous token in place if minting fails, so a transient outage is harmless.
    async def alien_token_loop():
        interval = ALIEN_TOKEN.get("refresh_seconds") or DEFAULT_ALIEN_REFRESH_SECONDS
        while True:
            await asyncio.sleep(interval)
            try:
                await rebuild_tls()
            except Exception as exc:
                print(f"alien token: refresh failed: {exc}", flush=True)

    alien = asyncio.create_task(alien_token_loop()) if ALIEN_TOKEN else None

    # uvicorn captures SIGTERM/SIGINT, then after graceful shutdown restores the
    # previous handler and re-raises -- SIGTERM's default would kill us before any
    # cleanup. Install our own handler (which uvicorn restores) to drop the sockets.
    def _on_signal(signum, _frame):
        _cleanup_sockets()
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, _on_signal)

    server = uvicorn.Server(uvicorn.Config(app, log_config=log_config))
    try:
        await server.serve(sockets=[sock])
    finally:
        rot.cancel()
        if sweeper is not None:
            sweeper.cancel()
        if alien is not None:
            alien.cancel()
        _cleanup_sockets()
        agent.close()
        ingest.close()


def fetch_from_agent(socket_path: Path, service: str) -> dict:
    """Connect to the agent socket and return its JSON reply for `service`."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(socket_path))
    except OSError as e:
        sys.exit(f"cannot reach proxy agent at {socket_path}: {e}\nIs the proxy running?")
    try:
        s.sendall((service + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf.decode() or "{}")


def token_main(argv=None) -> None:
    """`security-proxy-token <service>` -- print a per-service gate token (or port/addr).

    argv defaults to None so argparse reads sys.argv (console-script entry point);
    the `security-proxy token ...` subcommand passes sys.argv[2:] explicitly."""
    ap = argparse.ArgumentParser(
        prog="security-proxy-token",
        description="Fetch the proxy's current port and a per-service gate token from "
                    "the agent socket. Each service gets a distinct token, so a token "
                    "minted for one service cannot be used against another.")
    ap.add_argument("service", nargs="?", default="",
                    help="service (route) name to mint a token for")
    ap.add_argument("--socket", default=None, help=f"agent socket (default {DEFAULT_AGENT_SOCKET})")
    ap.add_argument("--config", default=None, help="read agent_socket from this config file")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--port", action="store_true", help="print only the port")
    g.add_argument("--addr", action="store_true", help="print http://127.0.0.1:<port>")
    g.add_argument("--hostport", action="store_true",
                   help="print 127.0.0.1:<port> (e.g. s3cmd host_base/host_bucket)")
    g.add_argument("--json", action="store_true", help="print the raw JSON response")
    a = ap.parse_args(argv)

    sp = a.socket
    if sp is None and a.config:
        sp = json.load(open(os.path.expanduser(a.config))).get("agent_socket")
    socket_path = Path(sp or DEFAULT_AGENT_SOCKET).expanduser()

    conn_only = a.port or a.addr or a.hostport
    data = fetch_from_agent(socket_path, "" if conn_only else a.service)
    if "error" in data:
        sys.exit(f"{data['error']}; known services: {data.get('services', [])}")
    if a.json:
        print(json.dumps(data))
    elif a.port:
        print(data["port"])
    elif a.addr:
        print(f"http://127.0.0.1:{data['port']}")
    elif a.hostport:
        print(f"127.0.0.1:{data['port']}")
    elif not a.service:
        sys.exit("a service name is required, e.g. `security-proxy-token nomad` "
                 "(use --port/--addr for connection info)")
    else:
        print(data["token"])


def mail_token_main(argv=None) -> None:
    """`security-proxy mail-token <account>` -- print a fresh OAuth2 access token.

    Resolves the proxy port + the broker route's gate token from the agent socket, asks
    the proxy to refresh, and prints just the `access_token` -- for use as an
    mbsync/isync (or mutt/neomutt) `PassCmd`. The client_secret and refresh_token never
    leave the proxy; only the ~1h access token is printed. Exits non-zero (nothing on
    stdout) on failure, so a mail client treats it as an auth error."""
    import urllib.error
    import urllib.request
    ap = argparse.ArgumentParser(
        prog="security-proxy mail-token",
        description="Fetch a short-lived OAuth2 access token from the proxy's mail "
                    "broker route (for an mbsync/isync PassCmd).")
    ap.add_argument("account", help="account name, as configured under the route's oauth.accounts")
    ap.add_argument("--service", default="mail",
                    help="route/service name of the oauth broker route (default: mail)")
    ap.add_argument("--socket", default=None, help=f"agent socket (default {DEFAULT_AGENT_SOCKET})")
    ap.add_argument("--config", default=None, help="read agent_socket from this proxy config file")
    a = ap.parse_args(argv)

    sp = a.socket
    if sp is None and a.config:
        sp = json.load(open(os.path.expanduser(a.config))).get("agent_socket")
    socket_path = Path(sp or DEFAULT_AGENT_SOCKET).expanduser()

    data = fetch_from_agent(socket_path, a.service)
    if "error" in data:
        sys.exit(f"{data['error']}; known services: {data.get('services', [])}")

    url = f"http://127.0.0.1:{data['port']}/{a.service}/{quote(a.account)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {data['token']}"})
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"token refresh failed (status {e.code}): {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach proxy at {url}: {e}")

    token = body.get("access_token")
    if not token:
        sys.exit(f"no access_token in proxy response: {body}")
    print(token)


DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
DEVICE_PROVIDERS = {
    "microsoft": {
        "devicecode": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode",
        "token": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "default_tenant": "organizations",
    },
    "google": {
        "devicecode": "https://oauth2.googleapis.com/device/code",
        "token": "https://oauth2.googleapis.com/token",
        "default_tenant": None,
    },
}


def _post_form(url: str, form: dict) -> tuple[int, dict]:
    """POST an x-www-form-urlencoded body; return (status, parsed-JSON). Uses stdlib
    urllib so the CLI has no third-party dependency."""
    import urllib.error
    import urllib.request
    data = urlencode(form).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"content-type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except ValueError:
            return e.code, {}


def device_authorize_main(argv=None) -> None:
    """`security-proxy device-authorize <provider>` -- run the OAuth2 device-code flow
    and print the resulting refresh token to stdout (prompts go to stderr).

    Intended as a `command` bootstrap source for an oauth route's refresh-token slot,
    so the one-time browser consent happens at provisioning time -- no oama needed.
    Caches the token (default ~/.config/security-proxy/<account>.refresh.json, 0600) and
    reuses it on later runs, so re-bootstrapping never re-prompts; pass --force to
    re-authorize. Device-code flow is for public clients, so no client_secret."""
    ap = argparse.ArgumentParser(
        prog="security-proxy device-authorize",
        description="Run the OAuth2 device-code flow and print the refresh token to "
                    "stdout (for use as a 'command' bootstrap source).")
    ap.add_argument("provider", choices=sorted(DEVICE_PROVIDERS),
                    help="OAuth provider preset (sets the device-code/token endpoints)")
    ap.add_argument("--client-id", required=True, help="public OAuth client id")
    ap.add_argument("--scope", required=True,
                    help="space-separated scopes; include offline_access for a refresh token")
    ap.add_argument("--tenant", default=None, help="Microsoft tenant (default: organizations)")
    ap.add_argument("--devicecode-url", default=None, help="override the device-code endpoint")
    ap.add_argument("--token-url", default=None, help="override the token endpoint")
    ap.add_argument("--account", default=None, help="label for the cache filename")
    ap.add_argument("--cache", default=None, help="cache file path (default under ~/.config/security-proxy)")
    ap.add_argument("--no-cache", action="store_true", help="do not read or write a cache")
    ap.add_argument("--force", action="store_true", help="ignore any cached token and re-authorize")
    a = ap.parse_args(argv)

    preset = DEVICE_PROVIDERS[a.provider]
    tenant = a.tenant or preset["default_tenant"] or "common"
    devicecode_url = a.devicecode_url or preset["devicecode"].format(tenant=tenant)
    token_url = a.token_url or preset["token"].format(tenant=tenant)

    cache_path = None
    if not a.no_cache:
        label = a.account or a.client_id
        cache_path = Path(a.cache).expanduser() if a.cache else \
            Path("~/.config/security-proxy").expanduser() / f"{label}.refresh.json"
        if not a.force:
            cached = _file_get_token(cache_path)
            if cached:
                print(cached)
                return

    # 1. Ask for a device + user code
    status, dc = _post_form(devicecode_url, {"client_id": a.client_id, "scope": a.scope})
    if status != 200 or "device_code" not in dc:
        sys.exit(f"device-code request failed ({status}): {dc.get('error_description', dc)}")
    prompt = dc.get("message") or (
        f"To authorize, visit {dc.get('verification_uri', dc.get('verification_url'))} "
        f"and enter code: {dc['user_code']}")
    print(prompt, file=sys.stderr, flush=True)

    # 2. Poll the token endpoint until the user approves (or it expires)
    interval = int(dc.get("interval", 5))
    deadline = time.monotonic() + int(dc.get("expires_in", 900))
    while time.monotonic() < deadline:
        time.sleep(interval)
        status, tok = _post_form(token_url, {
            "grant_type": DEVICE_GRANT, "client_id": a.client_id, "device_code": dc["device_code"],
        })
        if status == 200 and tok.get("refresh_token"):
            rt = tok["refresh_token"]
            if cache_path is not None:
                _file_set_token(cache_path, rt, token_url)
            print(rt)
            return
        err = tok.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        sys.exit(f"authorization failed: {tok.get('error_description', err or tok)}")
    sys.exit("device code expired before authorization completed")


def push_slot(socket_path: Path, slot: str, value: str | None, clear: bool = False) -> dict:
    """Push one secret value to a named slot over the write-only ingest socket.

    With clear=True, wipe the slot instead (used to close an attended window early)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(socket_path))
    except OSError as e:
        sys.exit(f"cannot reach ingest socket at {socket_path}: {e}\nIs the proxy running?")
    try:
        msg = {"slot": slot, "clear": True} if clear else {"slot": slot, "value": value}
        s.sendall((json.dumps(msg) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf.decode() or "{}")


def push_main(argv=None) -> None:
    """`security-proxy-push <slot>` -- read a secret value from stdin and push it.

    The value is read from stdin (never argv) so it never appears in shell history
    or the process list."""
    ap = argparse.ArgumentParser(
        prog="security-proxy-push",
        description="Push a secret value (read from stdin) into the running proxy's "
                    "named slot, over the write-only ingest socket.")
    ap.add_argument("slot", help="slot name to set")
    ap.add_argument("--socket", default=None, help=f"ingest socket (default {DEFAULT_INGEST_SOCKET})")
    ap.add_argument("--config", default=None, help="read ingest_socket from this proxy config file")
    a = ap.parse_args(argv)

    sp = a.socket
    if sp is None and a.config:
        sp = json.load(open(os.path.expanduser(a.config))).get("ingest_socket")
    socket_path = Path(sp or DEFAULT_INGEST_SOCKET).expanduser()

    value = sys.stdin.read().strip()
    if not value:
        sys.exit("no value on stdin")
    resp = push_slot(socket_path, a.slot, value)
    if not resp.get("ok"):
        sys.exit(f"push failed: {resp.get('error', resp)}")
    extra = f" (tls: {resp['tls']})" if "tls" in resp else ""
    print(f"pushed slot {a.slot!r}{extra}")


def _export_identity_pem(identity: str) -> str:
    """Export a macOS Keychain identity (grid cert + key) as one combined PEM string.

    Reuses the same p12 export + cryptography conversion the proxy's own
    keychain_identity path uses; the p12 lives only in a 0600 temp file briefly."""
    import cryptography.hazmat.primitives.serialization as ser
    from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates

    password = os.urandom(16).hex()
    tmp_dir = Path(tempfile.mkdtemp(prefix="security-proxy-"))
    p12 = tmp_dir / "identity.p12"
    try:
        export_keychain_identity(identity, p12, password)
        key, cert, chain = load_key_and_certificates(p12.read_bytes(), password.encode())
    finally:
        p12.unlink(missing_ok=True)
        tmp_dir.rmdir()
    if cert is None or key is None:
        raise RuntimeError(f"no exportable identity '{identity}' found in the Keychain")
    pem = cert.public_bytes(ser.Encoding.PEM)
    for ca in (chain or []):
        pem += ca.public_bytes(ser.Encoding.PEM)
    pem += key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption())
    return pem.decode()


def _resolve_source(source: dict) -> str | None:
    """Resolve a bootstrap source descriptor to a secret value.

    {"keychain": "<account>"} or {"keychain": {"service","account"}} (a generic
    password), {"keychain_identity": "<name>"} (a cert identity -> combined PEM), or
    {"command": "<shell>"} (its stdout is the value)."""
    if "keychain" in source:
        kc = source["keychain"]
        if isinstance(kc, str):
            kc = {"account": kc}
        return keychain_get_token(kc.get("service", "security-proxy"), kc["account"],
                                  kc.get("keychain"))
    if "keychain_identity" in source:
        return _export_identity_pem(source["keychain_identity"])
    if "command" in source:
        # Capture stdout (the value) but let stderr pass through to the terminal, so an
        # interactive source -- e.g. `security-proxy device-authorize`, which prints its
        # "visit this URL and enter this code" prompt to stderr -- is visible while
        # bootstrap waits for it.
        out = subprocess.run(source["command"], shell=True, stdout=subprocess.PIPE, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"command exited {out.returncode}")
        return out.stdout.strip()
    raise ValueError('source must have "keychain", "keychain_identity", or "command"')


def bootstrap_main(argv=None) -> None:
    """`security-proxy-bootstrap` -- fill the proxy's slots from a bootstrap config.

    The config (default ~/.security-proxy-bootstrap.json) maps each slot to a
    source: {"keychain": {...}} or {"command": "<shell>"}. Re-run after each proxy
    restart, since slots live only in the proxy's memory."""
    ap = argparse.ArgumentParser(
        prog="security-proxy-bootstrap",
        description="Provision the running proxy's secret slots from their configured "
                    "sources (macOS Keychain or an arbitrary command).")
    ap.add_argument("--config", default="~/.security-proxy-bootstrap.json",
                    help="bootstrap config mapping slot -> source (default ~/.security-proxy-bootstrap.json)")
    ap.add_argument("--socket", default=None, help=f"ingest socket (default {DEFAULT_INGEST_SOCKET})")
    a = ap.parse_args(argv)

    cfg_path = Path(a.config).expanduser()
    try:
        cfg = json.load(open(cfg_path))
    except OSError as e:
        sys.exit(f"cannot read bootstrap config {cfg_path}: {e}")
    slots = cfg.get("slots", cfg)  # accept {"slots": {...}} or a bare slot->source map
    socket_path = Path(a.socket or cfg.get("ingest_socket") or DEFAULT_INGEST_SOCKET).expanduser()

    ok = 0
    total = 0
    for slot, source in slots.items():
        # "attended" holds the on-demand sources for attended slots -- deliberately not
        # provisioned here, since the point is that they only enter the proxy when the
        # human runs `security-proxy-unlock`.
        if slot in ("ingest_socket", "attended") or not isinstance(source, dict):
            continue
        total += 1
        try:
            value = _resolve_source(source)
        except Exception as e:
            print(f"[{slot}] source error: {e}")
            continue
        if not value:
            print(f"[{slot}] source produced no value")
            continue
        resp = push_slot(socket_path, slot, value)
        if resp.get("ok"):
            extra = f" (tls: {resp['tls']})" if "tls" in resp else ""
            print(f"[{slot}] provisioned{extra}")
            ok += 1
        else:
            print(f"[{slot}] push failed: {resp.get('error')}")
    print(f"provisioned {ok}/{total} slot(s) from {cfg_path}")
    attended = cfg.get("attended") or {}
    if attended:
        print(f"attended slot(s) left locked: {', '.join(sorted(attended))} "
              f"(security-proxy-unlock <slot>)")


def unlock_main(argv=None) -> None:
    """`security-proxy-unlock` -- open an attended slot's window (or close it).

    The slot's source lives under "attended" in the bootstrap config and should point
    at a *locked* keychain, so resolving it raises the macOS password/Touch ID prompt.
    That prompt is the whole control: an agent running as this same user can invoke
    this command, but cannot answer the prompt, so it cannot open the window."""
    ap = argparse.ArgumentParser(
        prog="security-proxy-unlock",
        description="Provision a high-privilege 'attended' slot for a limited window. "
                    "Requires the human at the keyboard: the source keychain is locked, "
                    "so macOS prompts for your password or Touch ID.")
    ap.add_argument("slot", nargs="?", help="attended slot to unlock (omit to list them)")
    ap.add_argument("--lock", action="store_true",
                    help="wipe the slot now instead, closing its window early")
    ap.add_argument("--config", default="~/.security-proxy-bootstrap.json",
                    help="bootstrap config holding the 'attended' sources")
    ap.add_argument("--socket", default=None, help=f"ingest socket (default {DEFAULT_INGEST_SOCKET})")
    a = ap.parse_args(argv)

    cfg_path = Path(a.config).expanduser()
    try:
        cfg = json.load(open(cfg_path))
    except OSError as e:
        sys.exit(f"cannot read bootstrap config {cfg_path}: {e}")
    attended = cfg.get("attended") or {}
    socket_path = Path(a.socket or cfg.get("ingest_socket") or DEFAULT_INGEST_SOCKET).expanduser()

    if not a.slot:
        if not attended:
            sys.exit(f'no "attended" slots configured in {cfg_path}')
        print("attended slots:")
        for slot in sorted(attended):
            print(f"  {slot}")
        return
    if a.slot not in attended:
        sys.exit(f'no attended source for {a.slot!r} in {cfg_path} '
                 f'(known: {", ".join(sorted(attended)) or "none"})')

    if a.lock:
        resp = push_slot(socket_path, a.slot, None, clear=True)
        print(f"[{a.slot}] locked" if resp.get("ok") else f"[{a.slot}] failed: {resp.get('error')}")
        return

    try:
        value = _resolve_source(attended[a.slot])
    except Exception as e:
        sys.exit(f"[{a.slot}] source error: {e}")
    if not value:
        # A cancelled/failed Keychain prompt lands here: `security` exits non-zero and
        # keychain_get_token returns None. Say so, rather than a bare "no value".
        sys.exit(f"[{a.slot}] no value from source -- was the Keychain prompt cancelled?")
    resp = push_slot(socket_path, a.slot, value)
    if not resp.get("ok"):
        sys.exit(f"[{a.slot}] push failed: {resp.get('error')}")
    window = resp.get("attended")
    if not window:
        # The proxy does not consider this slot attended -- the window is not enforced.
        sys.exit(f"[{a.slot}] provisioned, but the proxy has no attended_slots policy for "
                 f"it: it will stay resident until restart. Add one to the deployed config.")
    limits = []
    if window.get("ttl"):
        limits.append(f"{window['ttl']}s")
    if window.get("max_uses"):
        limits.append(f"{window['max_uses']} use(s)")
    print(f"[{a.slot}] unlocked ({', '.join(limits)})")


def main():
    dispatch = {"token": token_main, "push": push_main, "bootstrap": bootstrap_main,
                "unlock": unlock_main, "mail-token": mail_token_main,
                "device-authorize": device_authorize_main}
    if len(sys.argv) > 1 and sys.argv[1] in dispatch:
        return dispatch[sys.argv[1]](sys.argv[2:])

    global ROUTES, AGENT_SOCKET_GID, INGEST_SOCKET_GID, CERT_SLOT, KEY_SLOT, ALIEN_TOKEN

    parser = argparse.ArgumentParser(
        description="Credential proxy for ALICE services",
        epilog="""
Routes can be provided via a config file (--config) or inline (--route).

Config file example (JSON):
  {
    "routes": [
      {"prefix": "/ccdb/", "upstream": "https://alice-ccdb.cern.ch"},
      {"prefix": "/ws/", "upstream": "wss://example.cern.ch", "websocket": true},
      {"prefix": "/bkp/", "upstream": "https://ali-bookkeeping.cern.ch",
       "sso": {"login_url": "https://ali-bookkeeping.cern.ch/?page=home", "keychain": true}},
      {"prefix": "/", "name": "nomad", "upstream": "https://alinomad.cern.ch",
       "websocket": true, "auth_header": "X-Nomad-Token",
       "inject_headers": {"X-Nomad-Token": {"ingest": "nomad"}}}
    ],
    "cert": "~/.globus/usercert.pem",
    "key": "~/.globus/userkey.pem",
    "cafile": "/etc/ssl/certs/ca-bundle.crt",
    "agent_socket": "~/.security-proxy/agent.sock",
    "ingest_socket": "~/.security-proxy/ingest.sock",
    "agent_socket_group": "_securityproxy_clients",
    "ingest_socket_group": "_securityproxy_provisioners",
    "secret_rotation_seconds": 86400
  }

Credential fields: "cert"+"key", "p12"+"p12_password", or "keychain_identity".
The client cert may instead be pushed in at runtime: set "cert": {"ingest": "<slot>"}
(and optionally "key": {"ingest": "<slot>"}; if omitted the cert slot must hold both
cert and key PEM). mTLS routes 503 until it is provisioned -- so the daemon can hold
no cert files on disk at all.
CLI arguments override config file values.

Authentication: the proxy binds a random 127.0.0.1 port and generates a
high-entropy master secret that rotates every "secret_rotation_seconds" (default
1 day; the previous secret stays valid for one window). Each route "name" (default:
its prefix) gets a distinct gate token = HMAC(master, name), so a token for one
service cannot be replayed against another. Clients read the port and a service's
token from a per-user UNIX socket ("agent_socket", mode 0600 in a 0700 dir):
  export NOMAD_ADDR=$(security-proxy-token --addr)
  export NOMAD_TOKEN=$(security-proxy-token nomad)
WebSocket clients may authenticate with the reserved subprotocol
"security-proxy-token.<token>"; the proxy strips it before forwarding any real
subprotocols upstream.
Cookie headers are not forwarded in either direction by default. Set
"allow_cookies": true on a route only if that upstream explicitly needs browser
cookies; otherwise localhost cookies could leak across the proxy boundary.
Route upstreams must be HTTPS (or WSS for websocket-capable routes); OAuth token
endpoints and SSO login URLs must be HTTPS.

For ALICE grid upstreams (e.g. CCDB) a long-lived grid/host certificate can be kept
out of the upstream leg entirely by minting a short-lived JAliEn token from it:
  "cert": {"ingest": "grid-cert"},
  "alien_token": {"endpoint": "wss://alice-jcentral.cern.ch:8097/websocket/json",
                  "refresh_seconds": 43200}
The configured certificate is then used ONLY to authenticate to JAliEn central; the
returned token certificate is what the proxy presents upstream, and it is re-minted
on the given interval (a failed refresh keeps the previous token rather than dropping
to an unauthenticated context). This is what `alien-token-init` does, with no alienpy
dependency.

A route may carry a "sign" object to become a local Ed25519 signer (used by alibuild
to sign reapi Action Cache entries): it holds the private seed and signs opaque bytes,
so the producer never holds the key.
  {"name": "alibuild-ac-sign", "prefix": "/sign/alibuild-ac",
   "sign": {"key": {"ingest": "alibuild-ac-sign-key"}}}
The slot holds the raw 32-byte Ed25519 seed, base64-encoded. Endpoints under the prefix:
  POST <prefix>          sign the raw request body -> {"keyid", "sig"} (base64 signature)
  GET  <prefix>/pubkey   publish the public key    -> {"keyid", "publicKey"} (base64 raw)
keyid = sha256(raw pubkey). The route terminates locally (no upstream, no TLS needed);
it signs exactly the bytes received (a "dumb signer") -- the gate token is the
authorization boundary, and binding a signature to an artifact is the client/verifier's
job, not the proxy's.

For a separate-user / LaunchDaemon deployment set "agent_socket_group": "<group>"
and "ingest_socket_group": "<group>" so each socket is group-accessible (0660) to
the right role. The legacy "socket_group" key still applies one group to both
sockets when the split keys are absent.

A route may carry an "sso" object for CERN-SSO upstreams (e.g. ALICE Bookkeeping).
At startup the proxy opens the login URL in a browser; paste the captured token
(or the redirected URL). The token is stored and injected into upstream requests.
  "sso": {"login_url": "...", "inject": "query"|"bearer", "param": "token",
          "keychain": true|"<service>", "keychain_account": "<account>",
          "token_cache": "~/.security-proxy/<name>.token.json"}
Token storage: "keychain" (macOS Keychain, default service "security-proxy",
account defaults to the upstream host) is preferred; "token_cache" stores it in a
0600 JSON file instead. Without either, the token is re-requested on every start.

For non-SSO upstreams that authenticate with a static header token (e.g. a Nomad
ACL token via "X-Nomad-Token"), a route may set:
  "auth_header": "<Header-Name>"        read the client's gate token from this header
                                        instead of "Authorization: Bearer". A route
                                        with auth_header is also *matched* by the
                                        presence of that header (not by path), so
                                        several such services (e.g. Nomad/Consul/Vault,
                                        all at /v1/...) can share one port, each picked
                                        by its own token header. The full path is
                                        forwarded unstripped; "prefix" is not needed.
  "inject_headers": {"<H>": {"ingest": "<slot>"}}
                                        fill upstream auth header <H> from the named
                                        secret <slot>. The proxy never reads secrets
                                        from disk or the Keychain; they are pushed in
                                        at runtime over the write-only ingest socket
                                        and held only in memory.

For S3 upstreams (which sign each request rather than send a bearer token), a route
may set "s3" to make the proxy sign:
  {"name": "s3", "upstream": "https://s3.cern.ch",
   "s3": {"access_key": {"ingest": "s3-access"}, "secret_key": {"ingest": "s3-secret"}}}
The client (e.g. s3cmd) sends UNSIGNED requests whose access-key-id is the gate token
(`security-proxy-token s3`) signed with a dummy secret; the proxy validates the gate
token, then re-signs with the real keys (SigV4, UNSIGNED-PAYLOAD) for the real host.
Such a route is matched by an AWS-style Authorization header, uses path-style
addressing, and reuses the region/service the client signed with.

For per-bucket S3 credentials, map each bucket to its own keypair with "buckets"
(selected by the first path segment); a flat access_key/secret_key, if also given,
is the default for buckets without an entry:
  "s3": {"buckets": {
           "alibuild-repo":   {"access_key": {"ingest": "s3-access-repo"},
                               "secret_key": {"ingest": "s3-secret-repo"}},
           "alibuild-mirror": {"access_key": {"ingest": "s3-access-mirror"},
                               "secret_key": {"ingest": "s3-secret-mirror"}}}}
One ~/.s3cfg then serves every bucket -- `s3cmd ls s3://<bucket>/` -- with the same
gate token; the proxy picks the real keypair per bucket. Example ~/.s3cfg
(path-style: host_bucket has no %(bucket)s):
  host_base   = %(security-proxy-token --socket <agent.sock> --hostport)
  host_bucket = %(security-proxy-token --socket <agent.sock> --hostport)
  access_key  = %(security-proxy-token --socket <agent.sock> s3)
  secret_key  = proxy-signs-this-is-ignored
  use_https = False
  signature_v2 = False

For email (mbsync/isync, mutt) that authenticates to IMAP over OAuth2/XOAUTH2, an
"oauth" route brokers the token refresh so the client_secret and long-lived
refresh_token stay in the proxy and the client only ever gets a ~1h access token:
  {"name": "mail", "oauth": {"accounts": {
     "me@gmail.com": {
       "endpoint": "https://oauth2.googleapis.com/token",
       "client_id": "<id>.apps.googleusercontent.com",
       "client_secret": {"ingest": "gmail-client-secret"},
       "refresh_token": {"ingest": "gmail-refresh-token"}}}}}
The client GETs /<route>/<account> (path selects the account) with the route's gate
token; the proxy POSTs the refresh_token grant and returns only the access token,
stripping any rotated refresh_token from the reply. Providers that rotate the refresh
token on each use (Microsoft/Outlook -- also needs "scope") should add a per-account
"refresh_store": "<path>": the rotated token is persisted there (owned by the daemon
user) and preferred over the ingest slot, so refreshes survive restarts. Obtain the
first refresh_token via a one-time browser consent (e.g. oama) and seed it into the
slot. mbsync .mbsyncrc:
  PassCmd "security-proxy mail-token me@gmail.com --socket <agent.sock>"

Provisioning: the proxy starts with empty slots and serves a route 503 until its
slot is filled. Push secrets in with either:
  printf %s "$TOKEN" | security-proxy-push <slot>   # raw value, read from stdin
  security-proxy-bootstrap                           # fill all slots from a config
The bootstrap config (~/.security-proxy-bootstrap.json) maps each slot to a source:
  {"nomad":     {"keychain": {"service": "security-proxy", "account": "alinomad.cern.ch"}},
   "grid-cert": {"keychain_identity": "My Grid Cert"},
   "other":     {"command": "op read op://vault/item/field"}}
Sources: "keychain" (generic password), "keychain_identity" (a cert identity, pushed
as combined cert+key PEM), or "command" (its stdout is the value). A "keychain"
source may name an explicit keychain file:
  {"keychain": {"service": "s", "account": "a",
                "keychain": "~/Library/Keychains/security-proxy.keychain-db"}}
Slots live only in memory, so re-run provisioning after each proxy (re)start.

Attended slots -- high-privilege secrets that must not sit resident:
  "attended_slots": {"vault-admin": {"ttl": 300, "max_uses": 1}}
Such a slot is skipped by bootstrap and its routes answer 403 until the human runs
`security-proxy-unlock vault-admin`, whose source (under "attended" in the bootstrap
config) should point at a *locked* keychain so macOS prompts for a password or Touch
ID. The proxy then wipes the value after "ttl" seconds and/or "max_uses" requests,
whichever comes first, so nothing unattended can use it. Close a window early with
`security-proxy-unlock <slot> --lock`. Bootstrap config:
  {"slots": {...},
   "attended": {"vault-admin": {"keychain": {"service": "vault-admin", "account": "me",
                "keychain": "~/Library/Keychains/security-proxy.keychain-db"}}}}

Routes without auth_header are matched by URL path prefix (longest first) -- for
browser/curl access. Header-matched routes take precedence over prefix routes.
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    default_config = Path("~/.security-proxy.json").expanduser()
    parser.add_argument("--config", default=str(default_config) if default_config.exists() else None,
                        help="Path to JSON config file containing routes (default: ~/.security-proxy.json if it exists)")
    parser.add_argument("--route", action="append",
                        help='Route definition as JSON: {"prefix": "/path/", "upstream": "https://...", "token": "..."}')

    cred_group = parser.add_mutually_exclusive_group()
    cred_group.add_argument("--cert", help="Path to client certificate (PEM); requires --key")
    cred_group.add_argument("--p12", help="Path to PKCS12 file (e.g. ~/.globus/usercert.p12)")
    cred_group.add_argument("--keychain-identity",
                            help="macOS Keychain identity name or SHA-1 fingerprint to export")

    parser.add_argument("--key", help="Path to client private key (PEM)")
    parser.add_argument("--p12-password", default=None, help="Password for the PKCS12 file")
    parser.add_argument("--cafile", default=None,
                        help="Path to CA bundle for verifying upstream")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    if not args.config and not args.route:
        parser.error("at least one of --config or --route is required")

    # Load config file and apply as defaults (CLI args override)
    config = {}
    route_defs = []
    if args.config:
        with open(args.config) as f:
            config = json.load(f)
        if "routes" in config:
            if not isinstance(config["routes"], list):
                parser.error("config 'routes' must be an array")
            route_defs.extend(config["routes"])

    # The client cert/key may be pushed in at runtime via ingest slots:
    #   "cert": {"ingest": "<slot>"}   (and optionally "key": {"ingest": "<slot>"};
    #   if "key" is omitted the cert slot must contain both cert and key).
    if isinstance(config.get("cert"), dict) and "ingest" in config["cert"]:
        CERT_SLOT = config["cert"]["ingest"]
        if isinstance(config.get("key"), dict) and "ingest" in config["key"]:
            KEY_SLOT = config["key"]["ingest"]

    # Apply config file defaults for credential/server options (file-based cert)
    if not args.cert and not args.p12 and not args.keychain_identity and not CERT_SLOT:
        if isinstance(config.get("cert"), str):
            args.cert = str(Path(config["cert"]).expanduser())
        if isinstance(config.get("key"), str):
            args.key = str(Path(config["key"]).expanduser())
        if isinstance(config.get("p12"), str) and not args.cert:
            args.p12 = str(Path(config["p12"]).expanduser())
        if "p12_password" in config and args.p12_password is None:
            args.p12_password = config["p12_password"]
        if isinstance(config.get("keychain_identity"), str) and not args.cert and not args.p12:
            args.keychain_identity = config["keychain_identity"]
    if args.cafile is None and "cafile" in config:
        args.cafile = str(Path(config["cafile"]).expanduser())
    if args.host is None:
        args.host = config.get("host", "127.0.0.1")
    if args.port is None:
        args.port = config.get("port", 8080)
    if args.p12_password is None:
        args.p12_password = ""

    if not args.cert and not args.p12 and not args.keychain_identity and not CERT_SLOT:
        parser.error('credentials required: --cert/--key, --p12, --keychain-identity, '
                     'or a {"ingest": "<slot>"} cert (via CLI or config file)')
    if args.cert and not args.key:
        parser.error("--cert requires --key")
    if args.route:
        route_defs.extend(json.loads(r) for r in args.route)

    for r in route_defs:
        route_is_websocket = r.get("websocket", False)
        if not isinstance(route_is_websocket, bool):
            parser.error("Route 'websocket' must be a boolean")
        # A `sign` route terminates locally (like the signing endpoint has no upstream);
        # every other route forwards and therefore needs a validated HTTPS/WSS upstream.
        if "sign" in r:
            route_upstream = ""
        elif "upstream" not in r:
            parser.error("Route missing required field: upstream")
        else:
            route_upstream = validate_absolute_url(
                parser,
                r["upstream"],
                "Route 'upstream'",
                {"https", "wss"} if route_is_websocket else {"https"},
            ).rstrip("/")
        # prefix is optional for header-matched routes (matched by auth_header, not path)
        prefix = r.get("prefix", "/")
        sso = r.get("sso")
        if sso is not None and not isinstance(sso, dict):
            parser.error("Route 'sso' must be an object")
        if sso is not None and "login_url" in sso:
            validate_absolute_url(parser, sso["login_url"], "Route 'sso.login_url'", {"https"})
        inject_headers = r.get("inject_headers")
        if inject_headers is not None and not isinstance(inject_headers, dict):
            parser.error("Route 'inject_headers' must be an object")
        ingest_headers = {}
        for header, val in (inject_headers or {}).items():
            if not (isinstance(val, dict) and isinstance(val.get("ingest"), str) and val["ingest"]):
                parser.error(f'inject_headers["{header}"] must be {{"ingest": "<slot>"}}; '
                             "secrets are pushed in at runtime, not stored in config")
            ingest_headers[header] = val["ingest"]
        # S3 signing route: the access/secret keys come from ingest slots, never config.
        s3_cfg = r.get("s3")
        s3_sign = None
        if s3_cfg is not None:
            if not isinstance(s3_cfg, dict):
                parser.error("Route 's3' must be an object")

            def _s3_keypair(obj, where):
                def _slot(field):
                    v = obj.get(field)
                    if not (isinstance(v, dict) and isinstance(v.get("ingest"), str) and v["ingest"]):
                        parser.error(f'{where}["{field}"] must be {{"ingest": "<slot>"}}; '
                                     "the S3 keys are pushed in at runtime, not stored in config")
                    return v["ingest"]
                return {"access_slot": _slot("access_key"), "secret_slot": _slot("secret_key")}

            # A flat access_key/secret_key is the default keypair (used for any bucket
            # without its own entry); "buckets" maps a bucket name to its own keypair,
            # so per-bucket S3 credentials are selected by the first path segment.
            default_kp = None
            if "access_key" in s3_cfg or "secret_key" in s3_cfg:
                default_kp = _s3_keypair(s3_cfg, "s3")
            buckets = {}
            bmap = s3_cfg.get("buckets")
            if bmap is not None:
                if not isinstance(bmap, dict):
                    parser.error("s3['buckets'] must be an object mapping bucket -> keypair")
                for bname, bobj in bmap.items():
                    if not isinstance(bobj, dict):
                        parser.error(f"s3['buckets']['{bname}'] must be an object")
                    buckets[bname] = _s3_keypair(bobj, f"s3['buckets']['{bname}']")
            if default_kp is None and not buckets:
                parser.error("Route 's3' needs access_key+secret_key and/or a 'buckets' map")
            # No default region: the proxy signs with the region the client sent (SigV4
            # always includes one). "region" is an explicit override for SigV2 clients.
            s3_sign = {
                "region": s3_cfg.get("region"),
                "service": s3_cfg.get("service", "s3"),
                "default": default_kp,
                "buckets": buckets,
            }
        # OAuth2 refresh-token broker route: the client_secret + refresh_token come from
        # ingest slots (never config), so mbsync/oama-style clients receive only a
        # short-lived access token. One route serves several accounts (selected by path).
        oauth_cfg = r.get("oauth")
        oauth = None
        if oauth_cfg is not None:
            if not isinstance(oauth_cfg, dict):
                parser.error("Route 'oauth' must be an object")
            accts = oauth_cfg.get("accounts")
            if not isinstance(accts, dict) or not accts:
                parser.error("Route 'oauth' needs a non-empty 'accounts' object")

            def _oauth_slot(obj, field, where):
                v = obj.get(field)
                if not (isinstance(v, dict) and isinstance(v.get("ingest"), str) and v["ingest"]):
                    parser.error(f'{where}["{field}"] must be {{"ingest": "<slot>"}}; '
                                 "OAuth secrets are pushed in at runtime, not stored in config")
                return v["ingest"]

            default_endpoint = oauth_cfg.get("endpoint")
            accounts = {}
            for aname, aobj in accts.items():
                where = f"oauth['accounts']['{aname}']"
                if not isinstance(aobj, dict):
                    parser.error(f"{where} must be an object")
                endpoint = aobj.get("endpoint") or default_endpoint
                endpoint = validate_absolute_url(
                    parser,
                    endpoint,
                    f"{where}['endpoint']",
                    {"https"},
                )
                client_id = aobj.get("client_id")
                if not isinstance(client_id, str) or not client_id:
                    parser.error(f"{where} needs a string 'client_id'")
                scope = aobj.get("scope")
                if scope is not None and not isinstance(scope, str):
                    parser.error(f"{where}['scope'] must be a string")
                store = aobj.get("refresh_store")
                if store is not None and not isinstance(store, str):
                    parser.error(f"{where}['refresh_store'] must be a path string")
                # client_secret is optional: confidential clients have one, public
                # clients (Microsoft/Google device-code flow) do not.
                secret_slot = (_oauth_slot(aobj, "client_secret", where)
                               if "client_secret" in aobj else None)
                accounts[aname] = {
                    "endpoint": endpoint,
                    "client_id": client_id,
                    "secret_slot": secret_slot,
                    "refresh_slot": _oauth_slot(aobj, "refresh_token", where),
                    "scope": scope,
                    "refresh_store": store,
                }
            oauth = {"accounts": accounts}
            # An oauth route is matched by URL path prefix (account = the rest of the
            # path); default the prefix to the route name so config can stay minimal.
            if "prefix" not in r:
                prefix = r.get("name") or "oauth"
        # Local Ed25519 signing route (alibuild AC): the seed comes from an ingest slot,
        # never config. Accept both {"key": {"ingest": "<slot>"}} (matches the s3/inject
        # convention) and a bare {"key_slot": "<slot>"} (a slot *name*, not a secret).
        sign_cfg = r.get("sign")
        sign = None
        if sign_cfg is not None:
            if not isinstance(sign_cfg, dict):
                parser.error("Route 'sign' must be an object")
            key = sign_cfg.get("key")
            if isinstance(key, dict) and isinstance(key.get("ingest"), str) and key["ingest"]:
                key_slot = key["ingest"]
            elif isinstance(sign_cfg.get("key_slot"), str) and sign_cfg["key_slot"]:
                key_slot = sign_cfg["key_slot"]
            else:
                parser.error('Route \'sign\' needs {"key": {"ingest": "<slot>"}} '
                             '(or a "key_slot": "<slot>" string)')
            sign = {"key_slot": key_slot}
            if "prefix" not in r:
                parser.error("Route 'sign' needs an explicit 'prefix' (e.g. /sign/alibuild-ac)")
        # Service name drives the per-service gate token; defaults to the prefix.
        name = r.get("name") or prefix.strip("/") or "default"
        allow_cookies = r.get("allow_cookies", False)
        if not isinstance(allow_cookies, bool):
            parser.error("Route 'allow_cookies' must be a boolean")
        ROUTES.append(Route(
            prefix=prefix.strip("/") + "/",
            upstream=route_upstream,
            token=r.get("token", ""),
            name=name,
            websocket=route_is_websocket,
            sso=sso,
            ingest_headers=ingest_headers or None,
            auth_header=r.get("auth_header"),
            s3_sign=s3_sign,
            oauth=oauth,
            sign=sign,
            allow_cookies=allow_cookies,
        ))
    # Service names must be unique, else gate tokens would be ambiguous
    names = [r.name for r in ROUTES]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        parser.error(f'duplicate route name(s) {dupes}; set a unique "name" per route')
    # Sort longest prefix first for correct matching
    ROUTES.sort(key=lambda r: -len(r.prefix))

    # Acquire SSO credentials up front (interactive login may prompt on the terminal).
    # Ingest-provided secrets (e.g. the Nomad ACL token) are pushed in at runtime.
    for route in ROUTES:
        if route.sso:
            route.injected_token = acquire_sso_token(route)

    app.state.args = args
    log_config = deepcopy(uvicorn.config.LOGGING_CONFIG)
    install_log_redaction(log_config)
    log_config["formatters"]["default"]["fmt"] = "%(asctime)s - %(levelprefix)s %(message)s"
    log_config["formatters"]["access"]["fmt"] = '%(asctime)s - %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'

    # Optional JAliEn token minting: the configured cert is used only to obtain a
    # short-lived token, which becomes the client cert for upstream mTLS.
    alien_cfg = config.get("alien_token")
    if alien_cfg is not None:
        if not isinstance(alien_cfg, dict):
            parser.error("'alien_token' must be an object")
        endpoint = alien_cfg.get("endpoint") or DEFAULT_ALIEN_ENDPOINT
        validate_absolute_url(parser, endpoint, "alien_token['endpoint']", {"wss"})
        refresh = alien_cfg.get("refresh_seconds", DEFAULT_ALIEN_REFRESH_SECONDS)
        if not isinstance(refresh, int) or refresh <= 0:
            parser.error("alien_token['refresh_seconds'] must be a positive integer")
        ALIEN_TOKEN = {"endpoint": endpoint, "refresh_seconds": refresh}

    attended_cfg = config.get("attended_slots")
    if attended_cfg is not None:
        if not isinstance(attended_cfg, dict):
            parser.error("'attended_slots' must be an object mapping slot -> policy")
        for slot, policy in attended_cfg.items():
            if not isinstance(policy, dict):
                parser.error(f"attended_slots['{slot}'] must be an object")
            ttl = policy.get("ttl", DEFAULT_ATTENDED_TTL)
            uses = policy.get("max_uses")
            if ttl is not None and (not isinstance(ttl, int) or ttl <= 0):
                parser.error(f"attended_slots['{slot}']['ttl'] must be a positive integer or null")
            if uses is not None and (not isinstance(uses, int) or uses <= 0):
                parser.error(f"attended_slots['{slot}']['max_uses'] must be a positive integer or null")
            if ttl is None and uses is None:
                parser.error(f"attended_slots['{slot}'] must set 'ttl' or 'max_uses': a window "
                             "that never closes is not attended")
            ATTENDED[slot] = {"ttl": ttl, "max_uses": uses}

    rotate_master()  # seed the initial gate secret before serving
    agent_path = Path(config.get("agent_socket", DEFAULT_AGENT_SOCKET)).expanduser()
    ingest_path = Path(config.get("ingest_socket", DEFAULT_INGEST_SOCKET)).expanduser()
    legacy_socket_group = config.get("socket_group")

    def socket_group_gid(config_key: str) -> int | None:
        grp_name = config.get(config_key)
        if grp_name is None:
            grp_name = legacy_socket_group
        if not grp_name:
            return None
        if not isinstance(grp_name, str):
            parser.error(f"{config_key} must be a group name string")
        try:
            return grp.getgrnam(grp_name).gr_gid
        except KeyError:
            parser.error(f"{config_key} '{grp_name}': no such group")

    AGENT_SOCKET_GID = socket_group_gid("agent_socket_group")
    INGEST_SOCKET_GID = socket_group_gid("ingest_socket_group")
    if agent_path.parent == ingest_path.parent and AGENT_SOCKET_GID != INGEST_SOCKET_GID:
        parser.error(
            "agent_socket and ingest_socket must be in different directories when their "
            "socket groups differ"
        )

    rotation = int(config.get("secret_rotation_seconds", DEFAULT_ROTATION_SECONDS))
    asyncio.run(serve(args, log_config, agent_path, ingest_path, rotation))


if __name__ == "__main__":
    main()
