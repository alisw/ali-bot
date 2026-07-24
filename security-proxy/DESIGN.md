# Security Proxy — design & trust model

This records the security decisions behind the proxy, so they aren't re-litigated.

## What it is

A localhost broker that holds real credentials (grid cert, Nomad ACL token, …) and
forwards to fixed upstreams, so clients use short-lived, per-service gate tokens
instead of the real secrets. See `README.md` for usage.

## The trust boundary

The human holds the privileged credentials — **sudo** and the **Keychain**.
Everything running as the user's normal account sits *outside* that boundary,
**including automated agents** (e.g. an AI assistant editing files). The design
assumes a same-uid process may be hostile or mistaken, and arranges that such a
process cannot, on its own:

- read the real secrets (they require the Keychain, which is the human's),
- change what the proxy does (routing/upstreams), or
- impersonate the daemon.

## Least privilege — expose the minimum to the assistant

Every configured route is *reachable* by any same-uid process that can fetch a gate
token — **including an AI assistant**. A gate token is not the real credential, but it
lets the holder exercise that credential against the route's upstream while the token
is valid. So each route widens what a hostile-or-mistaken agent can do *through* the
proxy. Treat the route set (and the Keychain items bootstrap reads) as an attack
surface and give the assistant the **bare minimum**:

- **Route only what you actually use;** delete routes you don't need — don't wire a
  service up "just in case."
- **Scope the credential, not the account:** prefer object-scoped, read-only, or
  short-lived keys over broad ones. **Never route admin/owner credentials** through the
  proxy — run those directly, by hand (see the admin-S3 note in `README.md`).
- **Prefer signing/injection over returning a usable token:** where the proxy signs
  (S3) or injects a header, the client never holds a reusable credential; where it must
  hand one back (an OAuth access token), scope and lifetime are your only bounds.
- **Adding a route is a privilege grant** to everything that can reach the sockets —
  decide it deliberately, per service.

## Secrets — pushed in, never on disk

The proxy never reads secrets from disk or the Keychain. Real upstream secrets — and
optionally the grid certificate itself — are pushed in at runtime over a **write-only
ingest socket** and held only in memory. A human-run bootstrap reads the Keychain (or
any source) and pushes them; the proxy stays secret-source-agnostic. This is what lets
the daemon run headless and as a *separate user* that has no Keychain of its own.

## Config integrity — prevention via filesystem ownership

Config (routes + upstreams) is **not** secret, but its *integrity* is critical: a
rerouted upstream would make the daemon replay your cert/token to an attacker's
server. We protect it by ownership, not by a runtime protocol:

- In the **separate-user / LaunchDaemon** deployment the config, code, venv and plist
  live in **root-owned** system paths (`/usr/local/etc|lib`, `/Library/LaunchDaemons`).
  A compromised user account simply **cannot write them** — changing config requires
  `sudo`, a different credential from the Keychain. This is *prevention*, not detection.
- Changes are applied by editing with `sudo` and restarting the daemon
  (`sudo launchctl kickstart -k system/ch.cern.security-proxy`).

### Rejected alternative: config-over-socket

We considered pushing config over the socket (write-once seal, persisted baseline,
diff-on-mismatch, optional "root token"). It was dropped: a root-owned static file
gives the same integrity guarantee by *prevention* with far less machinery, and the
"detect tampering across restarts" half was only ever as trustworthy as the integrity
of the baseline — which root ownership already provides directly.

## Working with the config: assistant drafts, human applies

Because integrity comes from the human-only `sudo`, **no same-uid process — including
an assistant — should edit the live daemon config.** That is the guarantee working,
not an obstacle. The workflow:

1. **Assistant authors** — writes a *candidate* config to a non-privileged path and
   shows the exact diff against the live one, with rationale.
2. **Human applies** — reviews, then `sudo cp` (or `sudo $EDITOR`) + restarts.

Same shape as secrets: the assistant prepares the non-secret wiring; the human runs
the privileged / Keychain-touching steps.

## Deployment modes

| | Same-user LaunchAgent (laptop) | Separate-user LaunchDaemon |
|---|---|---|
| Config integrity vs. compromised account | weak (config is yours, editable) | **strong** (root-owned) |
| Secrets on disk | none (ingest) | none (ingest) |
| Keychain access | direct, convenient | via human-run bootstrap |
| Socket privileges | private per-user sockets | split token-client and secret-provisioning groups |
| Use when | personal convenience | the integrity properties matter |

The strong properties above hold in the separate-user layout. The same-user layout
trades them for convenience and is the pragmatic default on a personal machine.
