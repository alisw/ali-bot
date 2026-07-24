# security-proxy `sign` route — spec for alibuild AC signing (S1)

A local-terminating route that Ed25519-signs opaque bytes with a proxy-held key.
The client is alibuild's `sign_via_proxy` (already implemented + tested). The
producer never holds the private key; the assistant never sees it.

## Frozen contract (locked by the alibuild client and its tests)

`alibuild_helpers/signing.py::sign_via_proxy` and the `_DumbSignerProxy` mock in
`tests/test_signing.py` already pin this down — the real route must match it:

- **Method / match:** `POST` to a dedicated route, e.g. prefix `/sign/alibuild-ac`.
- **Auth:** gate token via the *same* mechanism every route uses (`Authorization:
  Bearer <token>`, validated by the existing `presented_token` + master-HMAC path
  — do **not** add a new auth path). Service name e.g. `alibuild-ac-sign`.
- **Request body:** raw bytes = the DSSE PAE, already fully constructed by the
  client (`DSSEv1 <len> <type> <len> <payload>`). `Content-Type:
  application/octet-stream`. **Opaque to the proxy.**
- **Response `200`:** JSON `{"keyid": "<hex sha256 of the raw 32-byte pubkey>",
  "sig": "<base64 Ed25519 signature>"}`.
- **The proxy signs the exact bytes received** — no wrapping, re-encoding, or JSON
  parsing. The alibuild test `test_proxy_matches_local_sign_byte_for_byte` asserts
  the proxy sig equals a local `sign()` sig byte-for-byte; any transformation
  breaks it.
- **Errors:** `401` on missing/bad gate token; `503` if the key slot is
  unprovisioned (reuse the existing `SlotUnavailable` behaviour, like S3 routes).

## Key custody (human-driven, never in config)

- Ed25519 private seed = **32 raw bytes**, held in an ingest slot, e.g.
  `alibuild-ac-sign-key`. Referenced in config only as `{"ingest":
  "alibuild-ac-sign-key"}` — never a literal.
- Provisioned by you via `security-proxy-bootstrap` / `security-proxy-push` from
  the Keychain, exactly like the S3 creds. Add a source entry to
  `~/.security-proxy-bootstrap.json`.
- `keyid = sha256(raw_pubkey).hexdigest()`, derived from the seed at load time.

## Public-key export (to build the keyring)

Need the raw public key out so the alibuild keyring can be built. The pubkey is
public, so no secret is exposed. Recommended: `GET /sign/alibuild-ac/pubkey` →
`{"keyid", "publicKey": "<base64 raw 32-byte pubkey>"}` (gate-token or open —
either is fine). Self-serve keeps keyring builds reproducible. Alternative: a
one-liner you run from the seed.

## Implementation notes (FastAPI app)

- Add a `sign: dict | None = None` field to the `Route` dataclass, analogous to
  `s3_sign`: `{"key_slot": "alibuild-ac-sign-key"}`.
- Like `s3_sign`, this route **terminates locally** — there is no upstream, no
  mTLS, no cookies. Dispatch it before the forward path when `route.sign` is set
  (mirror how `s3_sign` is special-cased in `match_route` / the handler).
- Validate the gate token with the existing shared code path.
- Read the seed from `SLOTS[route.sign["key_slot"]]`; `503` if absent.
- Sign with `cryptography`'s `Ed25519PrivateKey.from_private_bytes(seed)` → return
  the JSON. (Confirm the proxy already imports `cryptography`; it should, for S3.)

## Config + rollout (in `setup-separate-user.sh` heredoc, then sudo redeploy)

- Add to the `routes` array:
  `{"name": "alibuild-ac-sign", "prefix": "/sign/alibuild-ac",
    "sign": {"key_slot": "alibuild-ac-sign-key"}}`
- The `alibuild-ac-sign` service token is derived from `name` automatically.
- Add the `alibuild-ac-sign-key` source to `~/.security-proxy-bootstrap.json`.
- `sudo bash ~/src/ali-bot/security-proxy/setup-separate-user.sh` then
  `security-proxy-bootstrap`.

## Key generation (human, one-time)

Generate an Ed25519 seed, store the raw 32 bytes (base64) in the Keychain under
the bootstrap-referenced identity. Export the pubkey (endpoint above) and add it
to the alibuild keyring with a `signer` label + validity window.

## Security properties / non-goals

- **Dumb signer:** it signs whatever PAE it is handed; the gate token is the
  authorization boundary and the key is single-purpose (AC signatures). Binding a
  signature to the *actual* artifact is enforced by `signed_payload()` on the
  client and by verification on consume (S3) — the proxy is **not** the policy
  point and never parses the AC entry. Canonicalization stays solely in
  `signing.py`.
- **Optional hardening (defer past MVP):** require the body to start with the
  expected PAE prefix (`DSSEv1 <n> application/vnd.alibuild.ac-signature.v1+json
  …`) so the key can't sign unrelated DSSE payload types; rate-limit; log
  keyid + payload prefix.

## Proxy-side test plan (hermetic — mirror the alibuild mock)

- Post known PAE bytes with a valid token → sig verifies under the pubkey; keyid
  matches `sha256(pubkey)`.
- Missing/bad token → `401`. Unprovisioned slot → `503`.
- `/pubkey` returns a keyid matching the one in signatures.
