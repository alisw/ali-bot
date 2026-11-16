"""Fake OIDC issuer (CERN SSO / Keycloak shaped) for test/sso_test.py.

    iss = FakeIssuer(); iss.start()          # thread, http://127.0.0.1:<port>/auth/realms/test
    tok = iss.mint(user="alice", roles=["alice-member"], aud="cocoindex-test")
    iss.rotate()                              # new RSA key, new kid, served next to the old one

Serves the discovery document, the JWKS (counting fetches), and a public PKCE
client's authorization-code flow: /auth answers at once with a redirect to
redirect_uri (the "logged-in" user is iss.login), /token checks the S256
verifier and returns an access token minted like the ones above. Signs RS256
with keys generated per run; nothing here is a real secret.
"""
import base64
import hashlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

REALM = "/auth/realms/test"
OIDC = REALM + "/protocol/openid-connect"


def new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeIssuer:
    def __init__(self, port: int = 0):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.issuer = self.url + REALM
        self.keys = {}               # kid -> private key; all are published
        self.kid = self.rotate()
        self.jwks_fetches = self.issued = 0
        self.login = {"user": "alice.member", "roles": ["alice-member"]}
        self.aud = "cocoindex-test"
        self._codes = {}

    def rotate(self) -> str:
        kid = "k" + secrets.token_hex(4)
        self.keys[kid] = new_key()
        self.kid = kid
        return kid

    def jwks(self) -> dict:
        keys = []
        for kid, k in self.keys.items():
            d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(k.public_key()))
            keys.append(dict(d, kid=kid, alg="RS256", use="sig"))
        return {"keys": keys}

    def claims(self, user="alice.member", roles=("alice-member",), aud=None, iss=None,
               ttl=300, nbf=None, typ="Bearer", **extra) -> dict:
        now = int(time.time())
        c = {"iss": iss or self.issuer, "aud": aud or self.aud, "sub": "f:" + user, "iat": now,
             "exp": now + ttl, "typ": typ, "azp": "cocoindex-test", "cern_upn": user,
             "preferred_username": user, "cern_roles": list(roles)}
        if nbf is not None:
            c["nbf"] = nbf
        c.update(extra)
        return c

    def mint(self, kid=None, key=None, alg="RS256", **kw) -> str:
        kid = kid or self.kid
        return jwt.encode(self.claims(**kw), key or self.keys[kid], algorithm=alg, headers={"kid": kid})

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, status=200):
                b = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                u = urlsplit(self.path)
                if u.path == REALM + "/.well-known/openid-configuration":
                    return self._json({
                        "issuer": fake.issuer, "jwks_uri": fake.url + OIDC + "/certs",
                        "authorization_endpoint": fake.url + OIDC + "/auth",
                        "token_endpoint": fake.url + OIDC + "/token",
                        "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
                        "grant_types_supported": ["authorization_code", "refresh_token"],
                        "token_endpoint_auth_methods_supported": ["client_secret_basic", "none"],
                        "scopes_supported": ["openid", "profile", "offline_access"],
                        "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"]})
                if u.path == OIDC + "/certs":
                    fake.jwks_fetches += 1
                    return self._json(fake.jwks())
                if u.path == OIDC + "/auth":
                    q = {k: v[0] for k, v in parse_qs(u.query).items()}
                    if q.get("code_challenge_method") != "S256" or q.get("response_type") != "code":
                        return self._json({"error": "invalid_request"}, 400)
                    code = secrets.token_urlsafe(16)
                    fake._codes[code] = (q["code_challenge"], q["redirect_uri"], q["client_id"])
                    self.send_response(302)
                    self.send_header("location", q["redirect_uri"] + "?" + urlencode(
                        {"code": code, "state": q.get("state", ""), "iss": fake.issuer}))
                    self.end_headers()
                    return
                self._json({"error": "not found"}, 404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0))).decode()
                q = {k: v[0] for k, v in parse_qs(body).items()}
                if urlsplit(self.path).path != OIDC + "/token" or q.get("grant_type") != "authorization_code":
                    return self._json({"error": "unsupported_grant_type"}, 400)
                challenge, redirect, client = fake._codes.pop(q.get("code"), (None, None, None))
                want = base64.urlsafe_b64encode(hashlib.sha256(q.get("code_verifier", "").encode()).digest())
                if challenge is None or want.rstrip(b"=").decode() != challenge or q.get("redirect_uri") != redirect:
                    return self._json({"error": "invalid_grant"}, 400)
                tok = fake.mint(user=fake.login["user"], roles=fake.login["roles"], azp=client)
                fake.issued += 1
                self._json({"access_token": tok, "token_type": "Bearer", "expires_in": 300})

        return H
