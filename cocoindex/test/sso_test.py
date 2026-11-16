"""CERN SSO (OIDC) auth of mcp_server.py, against test/fake_oidc.py.

    python test/sso_test.py <file:///.../bucket/cocoindex>

Same store and fake embedder as mcp_test.py, whose exercise() it reuses. Starts
an in-process fake issuer and three HTTP servers: "both" (token file + SSO),
"SSO only" behind the planned public URL, and misconfigured ones that must not
start. Covers: member accepted and every tool working; non-member 403;
expired / not-yet-valid / wrong issuer / wrong audience / bad signature /
alg=none / HS256-with-the-public-key / ID token -> 401; JWKS rotation and the
refetch floor; the WWW-Authenticate header and the RFC 9728 metadata; static
tokens in both modes; a full PKCE login through the SDK's OAuth client; user
names logged, tokens never.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import jwt
from cryptography.hazmat.primitives import serialization

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mcp_test as T  # noqa: E402  (reads the store from sys.argv[1])
from fake_oidc import FakeIssuer, new_key  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.client.auth import OAuthClientProvider  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared.auth import (AuthorizationCodeResult, OAuthClientInformationFull,  # noqa: E402
                             OAuthClientMetadata, ProtectedResourceMetadata)

check, free_port, payload = T.check, T.free_port, T.payload
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
ACCEPT = {"Accept": "application/json, text/event-stream"}


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unsigned(iss, alg, key=b""):
    """A token jwt.encode refuses to make: alg=none, or HS256 keyed with the RSA public key."""
    head = b64(json.dumps({"alg": alg, "kid": iss.kid, "typ": "JWT"}).encode())
    body = b64(json.dumps(iss.claims()).encode())
    sig = b64(hmac.new(key, f"{head}.{body}".encode(), hashlib.sha256).digest()) if key else ""
    return f"{head}.{body}.{sig}"


def post(base, token=None, **hdr):
    h = dict(ACCEPT, **hdr)
    if token:
        h["Authorization"] = f"Bearer {token}"
    return httpx.post(base + "/mcp", json=INIT, headers=h)


def start(work, name, env, port=None, wait=True):
    port = port or free_port()
    logf = work / f"{name}.log"
    p = subprocess.Popen([sys.executable, str(T.SERVER), "--http", "--port", str(port)],
                         env=env, stderr=open(logf, "w"))
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        if p.poll() is not None:
            break
        try:
            if httpx.get(base + "/healthz").status_code == 200 or not wait:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return p, base, logf


class Memory:
    """TokenStorage holding a pre-registered public client, as Claude Code's --client-id does."""

    def __init__(self, client):
        self.tokens, self.client = None, client

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, t):
        self.tokens = t

    async def get_client_info(self):
        return self.client

    async def set_client_info(self, c):
        self.client = c


async def pkce_session(base, iss, seen):
    redirect = "http://localhost:33418/callback"
    meta = OAuthClientMetadata(redirect_uris=[redirect], token_endpoint_auth_method="none",
                               grant_types=["authorization_code"], response_types=["code"], client_name="t")
    client = OAuthClientInformationFull(client_id="cocoindex-test", **meta.model_dump(exclude_none=True))
    result = {}

    async def on_redirect(url):  # the "browser": the fake issuer logs in at once
        seen["authorize"] = url
        loc = httpx.get(url, follow_redirects=False).headers["location"]
        result.update({k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()})

    async def on_callback():
        return AuthorizationCodeResult(**result)

    auth = OAuthClientProvider(base + "/mcp", meta, Memory(client), on_redirect, on_callback)
    async with httpx2.AsyncClient(auth=auth, timeout=60) as hc:
        async with Client(streamable_http_client(base + "/mcp", http_client=hc)) as c:
            return payload(await c.call_tool("index_status", {}))


async def main():
    work = Path(tempfile.mkdtemp(prefix="coco-sso-"))
    eport = free_port()
    emb = subprocess.Popen([sys.executable, str(HERE / "fake_embedder.py"), str(eport), str(work / "embed.log")])
    iss = FakeIssuer().start()
    tok_file = work / "tokens.json"
    tok_file.write_text(json.dumps({"ci-bot": hashlib.sha256(b"s3cret").hexdigest()}))
    env = dict(os.environ, COCO_STORE=T.STORE, OPENAI_API_BASE=f"http://127.0.0.1:{eport}/v1",
               OPENAI_API_KEY="fake", SQL_TIMEOUT_S="2", PYTHONUNBUFFERED="1")
    sso = dict(OIDC_ISSUER=iss.issuer, OIDC_AUDIENCE="some-other-client,cocoindex-test",
               OIDC_REQUIRED_ROLE="alice-member", OIDC_JWKS_MIN_REFRESH_S="2")
    minted = []

    def mint(**kw):
        minted.append(iss.mint(**kw))
        return minted[-1]

    procs = []
    try:
        # ---- misconfigurations refuse to start
        for what, extra in [("neither tokens nor SSO", {}),
                            ("SSO without OIDC_AUDIENCE", dict(sso, OIDC_AUDIENCE="", MCP_PUBLIC_URL="http://x/mcp")),
                            ("SSO without MCP_PUBLIC_URL", sso)]:
            r = subprocess.run([sys.executable, str(T.SERVER), "--http", "--port", str(free_port())],
                               env=dict(env, COCO_CACHE=str(work / "c0"), **extra), capture_output=True, text=True,
                               timeout=60)
            check(r.returncode != 0 and ("needs" in r.stderr or "is not" in r.stderr),
                  f"[config] refuses to start with {what}: {r.stderr.strip().splitlines()[-1]!r}")

        # ---- "both": token file and SSO, public URL = what the client dials
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        cache = work / "cache-both"
        p, base, log_both = start(work, "both", dict(env, **sso, COCO_CACHE=str(cache), MCP_TOKENS_FILE=str(tok_file),
                                                     MCP_PUBLIC_URL=base + "/mcp"), port)
        procs.append(p)
        prm_url = base + "/.well-known/oauth-protected-resource/mcp"
        check(httpx.get(base + "/healthz").status_code == 200, "[both] /healthz answers without a token")
        docs = [httpx.get(base + "/.well-known/oauth-protected-resource" + s) for s in ("/mcp", "")]
        check(all(d.status_code == 200 for d in docs) and docs[0].json() == docs[1].json(),
              "[both] metadata served without a token, at the path-suffixed and the root well-known URL")
        prm = docs[0].json()
        ProtectedResourceMetadata.model_validate(prm)
        check(prm["resource"] == base + "/mcp" and prm["authorization_servers"][0] == iss.issuer
              and prm["bearer_methods_supported"] == ["header"] and prm["scopes_supported"] == ["openid"],
              f"[both] metadata: resource = MCP_PUBLIC_URL, issuer first, scopes openid: {prm}")
        check(docs[0].headers.get("access-control-allow-origin") == "*", "[both] metadata allows CORS")

        r = post(base)
        check(r.status_code == 401 and r.headers["www-authenticate"] == f'Bearer resource_metadata="{prm_url}"',
              f"[both] no token -> 401, {r.headers.get('www-authenticate')}")
        n0 = iss.jwks_fetches
        r = post(base, mint())
        check(r.status_code == 200, f"[both] member's token accepted ({r.status_code})")
        check(iss.jwks_fetches == n0 + 1, f"[both] JWKS fetched once, through discovery ({iss.jwks_fetches - n0})")
        r = post(base, mint())
        check(r.status_code == 200 and iss.jwks_fetches == n0 + 1, "[both] JWKS cached: no fetch for a known kid")
        r = post(base, mint(user="bob.outsider", roles=["some-other-role"]))
        check(r.status_code == 403 and "alice-member" in r.text and "bob.outsider" in r.text
              and "insufficient_scope" not in r.headers.get("www-authenticate", ""),
              f"[both] non-member -> 403, no step-up invitation: {r.text.strip()!r}")
        r = post(base, mint(user="carol.noroles", roles=[], cern_roles=None))
        check(r.status_code == 403, "[both] token with no roles claim -> 403")

        pub = iss.keys[iss.kid].public_key().public_bytes(serialization.Encoding.PEM,
                                                          serialization.PublicFormat.SubjectPublicKeyInfo)
        now = int(time.time())
        bad = {"expired (beyond the leeway)": mint(ttl=-120),
               "not yet valid (nbf)": mint(nbf=now + 600),
               "wrong issuer": mint(iss="https://auth.example.org/auth/realms/cern"),
               "wrong audience": mint(aud="another-app"),
               "bad signature (same kid, other key)": mint(key=new_key()),
               "alg=none": unsigned(iss, "none"),
               "HS256 keyed with the public key": unsigned(iss, "HS256", pub),
               "an ID token, not an access token": mint(typ="ID"),
               "garbage with two dots": "a.b.c"}
        minted += [bad["alg=none"], bad["HS256 keyed with the public key"]]
        for what, tok in bad.items():
            r = post(base, tok)
            www = r.headers.get("www-authenticate", "")
            check(r.status_code == 401 and 'error="invalid_token"' in www and f'resource_metadata="{prm_url}"' in www,
                  f"[both] {what} -> 401 ({r.status_code}, {r.text.strip()[:70]!r})")
        check(post(base, mint(ttl=-10)).status_code == 200, "[both] expired 10 s ago is within the 30 s leeway")

        # ---- key rotation: a new kid makes the server refetch, made-up ones do not (floor)
        time.sleep(2.2)
        n0 = iss.jwks_fetches
        iss.rotate()
        r = post(base, mint())
        check(r.status_code == 200 and iss.jwks_fetches == n0 + 1,
              f"[both] rotated key: new kid accepted after one refetch ({iss.jwks_fetches - n0})")
        r1 = post(base, mint(kid="made-up-1", key=iss.keys[iss.kid]))
        r2 = post(base, mint(kid="made-up-2", key=iss.keys[iss.kid]))
        check(r1.status_code == r2.status_code == 401 and iss.jwks_fetches == n0 + 1,
              f"[both] unknown kids within the floor -> 401 without refetching ({iss.jwks_fetches - n0})")

        # ---- static token still works in "both"
        check(post(base, "s3cret").status_code == 200, "[both] static token accepted")
        check(post(base, "nope").status_code == 401, "[both] unknown static token -> 401")

        # ---- a whole session through the real MCP client with an SSO token: every tool
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {mint()}"}, timeout=60) as hc:
            async with Client(streamable_http_client(base + "/mcp", http_client=hc)) as c:
                await T.exercise(c, "sso", cache)

        # ---- the full login, as Claude Code does it: 401 -> metadata -> discovery -> PKCE -> token
        seen = {}
        iss.login = {"user": "dave.pkce", "roles": ["alice-member"]}
        st = await pkce_session(base, iss, seen)
        q = parse_qs(urlsplit(seen.get("authorize", "")).query)
        check(isinstance(st, dict) and st.get("repos"), "[pkce] SDK OAuth client logs in and calls a tool")
        check(q.get("client_id") == ["cocoindex-test"] and q.get("code_challenge_method") == ["S256"]
              and q.get("resource") == [base + "/mcp"] and q.get("scope") == ["openid"],
              f"[pkce] authorize request: pre-registered client, S256, resource, scope {q.get('scope')}")
        iss.login = {"user": "erin.pkce", "roles": []}
        n0 = iss.issued
        try:
            r = await asyncio.wait_for(pkce_session(base, iss, {}), 60)
            check(False, f"[pkce] non-member's session must fail, got {str(r)[:80]}")
        except Exception as e:
            while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:
                e = e.exceptions[0]
            check(iss.issued == n0 + 1, f"[pkce] non-member: one login, then an error, not a re-login loop "
                  f"({iss.issued - n0} token(s); {type(e).__name__}: {e!s:.60})")

        # ---- "SSO only", behind the planned public URL
        cache = work / "cache-sso"
        p, base2, log_sso = start(work, "sso-only", dict(env, **sso, COCO_CACHE=str(cache),
                                                         MCP_PUBLIC_URL="https://ali-ci.cern.ch/alice-mcp/mcp",
                                                         MCP_ALLOWED_HOSTS="ali-ci.cern.ch"))
        procs.append(p)
        r = httpx.get(base2 + "/.well-known/oauth-protected-resource/alice-mcp/mcp")
        check(r.status_code == 200 and r.json()["resource"] == "https://ali-ci.cern.ch/alice-mcp/mcp",
              "[sso-only] metadata at the path Traefik forwards unstripped: /.well-known/oauth-protected-resource/alice-mcp/mcp")
        r = post(base2)
        check(r.headers.get("www-authenticate") == 'Bearer resource_metadata="https://ali-ci.cern.ch/.well-known/'
              'oauth-protected-resource/alice-mcp/mcp"', f"[sso-only] 401 points at the public metadata URL")
        check(post(base2, "s3cret").status_code == 401, "[sso-only] static token refused")
        check(post(base2, mint(), Host="ali-ci.cern.ch").status_code == 200, "[sso-only] member's token accepted")
        check(post(base2, mint(), Host="evil.example").status_code in (403, 421), "[sso-only] foreign Host still refused")
    finally:
        for p in procs:
            p.terminate()
            p.wait()
        iss.stop()
        emb.terminate()

    logs = log_both.read_text() + log_sso.read_text()
    check("alice.member POST /mcp via=sso" in logs and "dave.pkce POST /mcp via=sso" in logs,
          "[logs] SSO callers logged by user name, with the mechanism")
    check("ci-bot POST /mcp via=token" in logs, "[logs] static-token callers logged by name, with the mechanism")
    check("bob.outsider" in logs and "lacks the 'alice-member' role" in logs, "[logs] refused non-member logged")
    leaked = [t for t in minted if t in logs or (t.rsplit(".", 1)[-1] or t) in logs]
    check(not leaked and "s3cret" not in logs, f"[logs] no token ({len(minted)} minted) and no signature ever logged")
    print(f"\n{len(T.failures)} failure(s)")
    sys.exit(1 if T.failures else 0)


asyncio.run(main())
