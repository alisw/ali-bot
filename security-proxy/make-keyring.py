#!/usr/bin/env python3
"""Build / update an alibuild AC signing keyring (the JSON consumed by
`aliBuild ... --trusted-keys keyring.json`).

The keyring is a flat map of *public* keys trusted to sign Action Cache entries; it
lives in alidist and is read fresh on every build, so changing the signer set is a
one-file edit, never an alibuild redeploy. Each entry's id is self-certifying
(`keyid == sha256(raw pubkey)`), which this tool computes -- never hand-write it.

Subcommands
-----------
  keygen  Generate a NEW Ed25519 signing key. Prints the private seed (base64) to
          stdout for you to store (Keychain / Vault); appends the *public* entry to
          the keyring. Run this yourself -- the seed is secret and must not be logged.
              python3 make-keyring.py keygen --signer alice-laptop --keyring keyring.json \
                | security add-generic-password -U -s 'alibuild-ac-sign-key' -a "$USER" -w
          (bare `-w` reads the seed from stdin; the seed never lands on disk.)

  add     Add an existing signer's PUBLIC key to the keyring, either fetched from a
          running proxy's /pubkey endpoint (no secret involved) or given directly:
              python3 make-keyring.py add --keyring keyring.json --signer alice-node1 \
                --from-proxy --socket /usr/local/var/run/security-proxy/agent/agent.sock
              python3 make-keyring.py add --keyring keyring.json --signer x --pubkey <base64>

  revoke  Add a keyid to the "revoked" list (kept in "keys" too, so verifiers still
          know who it was): python3 make-keyring.py revoke --keyring keyring.json --keyid <hex>

  show    Print the keyring's signers, keyids and validity windows.

Validity windows (--not-before / --not-after, ISO-8601, e.g. 2027-01-01T00:00:00Z) are
optional; pre-add a key with a future notBefore so a new signer is trusted the moment it
comes up without a last-minute keyring edit.
"""
import argparse
import base64
import hashlib
import json
import os
import socket
import sys
import urllib.request
from pathlib import Path


def keyid_for(pub_raw: bytes) -> str:
    """Self-certifying key id -- must match alibuild_helpers.signing.keyid_for."""
    return hashlib.sha256(pub_raw).hexdigest()


def _pub_from_seed(seed: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    sk = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    return sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _load(path: Path) -> dict:
    data = json.loads(path.read_text()) if path.exists() else {}
    data.setdefault("keys", {})
    data.setdefault("revoked", [])
    return data


def _save(path: Path, data: dict) -> None:
    # Deterministic output so keyring diffs in alidist are clean and reviewable.
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _validate(pub_b64: str) -> tuple[str, bytes]:
    pub = base64.b64decode(pub_b64, validate=True)
    if len(pub) != 32:
        sys.exit(f"public key must be 32 raw bytes, got {len(pub)}")
    return keyid_for(pub), pub


def _add_entry(data: dict, pub_b64: str, signer: str, not_before, not_after) -> str:
    keyid, _ = _validate(pub_b64)
    entry = {"publicKey": pub_b64, "signer": signer}
    if not_before:
        entry["notBefore"] = not_before
    if not_after:
        entry["notAfter"] = not_after
    if keyid in data["keys"] and data["keys"][keyid] != entry:
        print(f"note: replacing existing entry for {keyid[:16]}", file=sys.stderr)
    data["keys"][keyid] = entry
    return keyid


def _agent_query(socket_path: str, query: str) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(socket_path)
    except OSError as exc:
        sys.exit(f"cannot reach proxy agent at {socket_path}: {exc}")
    try:
        s.sendall((query + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf.decode() or "{}")


def _fetch_pubkey_from_proxy(socket_path: str, service: str, prefix: str) -> str:
    """Resolve port + gate token from the agent socket, GET the route's /pubkey.

    The proxy decouples a route's *service name* (which the gate token is minted for)
    from its URL *prefix* (the path it is served under), so both are needed: the token
    comes from `service`, the URL from `prefix`."""
    port = _agent_query(socket_path, "").get("port")
    tok_reply = _agent_query(socket_path, service)
    if "error" in tok_reply:
        sys.exit(f"{tok_reply['error']}; known services: {tok_reply.get('services', [])}")
    url = f"http://127.0.0.1:{port}/{prefix.strip('/')}/pubkey"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok_reply.get('token')}"})
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read().decode())
    keyid, _ = _validate(body["publicKey"])
    if keyid != body.get("keyid"):
        sys.exit(f"proxy keyid {body.get('keyid')} != sha256(publicKey) {keyid}")
    return body["publicKey"]


def main() -> None:
    ap = argparse.ArgumentParser(description="Build/update an alibuild AC signing keyring")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("keygen", help="generate a new key; seed -> stdout, public -> keyring")
    g.add_argument("--signer", required=True, help="signer label for the keyring entry")
    g.add_argument("--keyring", required=True, help="keyring JSON to update (created if absent)")
    g.add_argument("--not-before", default=None)
    g.add_argument("--not-after", default=None)

    a = sub.add_parser("add", help="add an existing signer's public key")
    a.add_argument("--signer", required=True)
    a.add_argument("--keyring", required=True)
    a.add_argument("--not-before", default=None)
    a.add_argument("--not-after", default=None)
    src = a.add_mutually_exclusive_group(required=True)
    src.add_argument("--pubkey", help="base64 raw 32-byte Ed25519 public key")
    src.add_argument("--from-proxy", action="store_true", help="fetch /pubkey from a running proxy")
    a.add_argument("--socket", help="agent socket (with --from-proxy)")
    a.add_argument("--service", default="alibuild-ac-sign",
                   help="sign route's service name (mints the gate token)")
    a.add_argument("--prefix", default="sign/alibuild-ac",
                   help="sign route's URL prefix (/pubkey is fetched under it)")

    rv = sub.add_parser("revoke", help="mark a keyid revoked")
    rv.add_argument("--keyring", required=True)
    rv.add_argument("--keyid", required=True)

    sh = sub.add_parser("show", help="list keyring entries")
    sh.add_argument("--keyring", required=True)

    args = ap.parse_args()
    path = Path(args.keyring)

    if args.cmd == "keygen":
        seed = os.urandom(32)
        pub = _pub_from_seed(seed)
        data = _load(path)
        keyid = _add_entry(data, base64.b64encode(pub).decode(), args.signer,
                           args.not_before, args.not_after)
        _save(path, data)
        print(f"added signer '{args.signer}' keyid {keyid} to {path}", file=sys.stderr)
        print("store the seed below (base64) in Keychain/Vault; it is NOT written to disk",
              file=sys.stderr)
        sys.stdout.write(base64.b64encode(seed).decode())   # secret: stdout only
        return

    if args.cmd == "add":
        if args.from_proxy:
            if not args.socket:
                sys.exit("--from-proxy requires --socket <agent.sock>")
            pub_b64 = _fetch_pubkey_from_proxy(args.socket, args.service, args.prefix)
        else:
            pub_b64 = args.pubkey
        data = _load(path)
        keyid = _add_entry(data, pub_b64, args.signer, args.not_before, args.not_after)
        _save(path, data)
        print(f"added signer '{args.signer}' keyid {keyid} to {path}", file=sys.stderr)
        return

    if args.cmd == "revoke":
        data = _load(path)
        if args.keyid not in data["keys"]:
            print(f"warning: {args.keyid} not present in keys", file=sys.stderr)
        if args.keyid not in data["revoked"]:
            data["revoked"].append(args.keyid)
        _save(path, data)
        print(f"revoked {args.keyid} in {path}", file=sys.stderr)
        return

    if args.cmd == "show":
        data = _load(path)
        revoked = set(data["revoked"])
        for keyid, e in sorted(data["keys"].items(), key=lambda kv: kv[1].get("signer", "")):
            window = " ".join(x for x in (e.get("notBefore", ""), e.get("notAfter", "")) if x)
            flag = "  REVOKED" if keyid in revoked else ""
            print(f"{e.get('signer','?'):20s} {keyid}  {window}{flag}")
        return


if __name__ == "__main__":
    main()
