# Security Proxy

A localhost credential proxy for ALICE services. It accepts requests carrying a
per-service gate token, then forwards them to the appropriate upstream using a
real grid certificate for TLS client authentication (and, per route, a static
auth header such as a Nomad ACL token). Both HTTP and WebSocket upstreams are
supported.

Clients never hold the real credentials: the proxy binds a random localhost port
and mints a high-entropy, daily-rotating gate token per service, handed out over a
per-user UNIX socket (mode 0600). Fetch them with `security-proxy-token`.

The proxy itself never reads secrets from disk or the Keychain. Real upstream
secrets are **pushed in at runtime** over a separate write-only ingest socket and
held only in memory — so the proxy can run headless or as a different user than the
one holding the secrets.

See [`DESIGN.md`](DESIGN.md) for the trust model and why config integrity is handled
by root ownership (edited via sudo) rather than a runtime protocol.

## Installation

```bash
cd <path-to>/security-proxy
python3 -m venv venv
./venv/bin/pip install -e .
```

This installs four commands into the venv: `security-proxy` (the daemon),
`security-proxy-token` (read a gate token), `security-proxy-push` (push one secret
from stdin), and `security-proxy-bootstrap` (push all secrets from a config).

## Tests

```bash
./venv/bin/python tests/run_all.py      # everything
./venv/bin/python tests/test_paths.py   # or one module
```

No pytest, no dependencies beyond the proxy's own venv; each module exits non-zero on
failure. They pin the security-relevant behaviour rather than the plumbing:

- `test_paths.py` — a route's upstream path is a *scope*, and `..` (raw, percent-encoded
  or backslash-separated) must not escape it and reach the rest of that host with the
  proxy's credential attached.
- `test_attended.py` — an attended secret is absent outside a window a human opened: use
  budget, TTL, the sweeper that drops an untouched value on time, and the 403-vs-503
  split that tells a caller which fault it hit.
- `test_sources.py` — bootstrap sources stay declarative; the removed `command` kind
  stays rejected, with a hint pointing at `file` / `vault` / `device_authorize`.

`test_sources.py` also checks `~/.security-proxy-bootstrap.json` when there is one —
that every slot resolves to exactly one known kind and every attended slot is sourced
from an explicit locked keychain. It skips that part on a machine without the file.

## Configuration

Routes and credentials live in `~/.security-proxy.json` (run `security-proxy --help`
for the full schema). Minimal example:

```json
{
  "cert": "~/.globus/usercert.pem",
  "key": "~/.globus/userkey.pem",
  "cafile": "~/.globus/cern-ca-bundle.pem",
  "routes": [
    {"prefix": "/ccdb/", "upstream": "https://alice-ccdb.cern.ch"},
    {"prefix": "/", "name": "nomad", "upstream": "https://alinomad.cern.ch",
     "websocket": true, "auth_header": "X-Nomad-Token",
     "inject_headers": {"X-Nomad-Token": {"ingest": "nomad"}}}
  ]
}
```

A route's `inject_headers` value is a slot reference (`{"ingest": "<slot>"}`) — the
config never contains a secret. The named slot is filled at runtime (see
*Provisioning secrets* below); until it is, that route returns `503`.

Several routes may share one `auth_header`, which is how a CLI that sends a single
fixed header gets more than one privilege level (read-only `nomad` and attended
`nomad-rw` both read `X-Nomad-Token`, since the Nomad CLI sends nothing else). Such
routes are told apart by the gate token, which is per-route; a token that matches no
candidate falls back to the first one listed, so list the least-privileged route first
— it is the one that will answer `401`. The daemon logs any shared header at startup.

**Least privilege — give the AI the bare minimum.** Any same-uid process that can
reach the sockets, **including an AI assistant**, can fetch a gate token and exercise
whatever a route exposes. So each route widens what such an agent can do through the
proxy. Route only what you actually use, scope credentials narrowly (object-scoped /
read-only / short-lived, never admin), prefer routes that sign or inject over ones
that hand back a usable token, and remove routes you no longer need. See the trust
model in [`DESIGN.md`](DESIGN.md).

Browser cookies are not forwarded across the proxy boundary by default: client
`Cookie` headers are stripped before upstream requests, and upstream `Set-Cookie`
headers are stripped before client responses. Set `"allow_cookies": true` on a
route only if that upstream explicitly requires cookies.

Route `upstream` URLs must use `https://` by default, or `https://`/`wss://` for
websocket-capable routes. OAuth token endpoints and SSO `login_url` values must
use `https://`.

## Provisioning secrets

The proxy starts with empty slots. Fill them after it is up — and again after each
restart, since slots live only in memory.

```bash
# all slots at once, from a bootstrap config
security-proxy-bootstrap

# or one slot, value piped from stdin (never argv, so never in shell history)
printf %s "$TOKEN" | security-proxy-push nomad
```

`security-proxy-bootstrap` reads `~/.security-proxy-bootstrap.json`, which maps each
slot to a **source**. Every kind is declarative — it names *what* to read, never a
command to run:

```json
{
  "nomad": {"keychain": {"service": "security-proxy", "account": "alinomad.cern.ch"}},
  "grid-cert": {"file": ["~/.globus/usercert.pem", "~/.globus/userkey.pem"]},
  "gitlab": {"vault": {"path": "kv/data/ci", "field": "gitlab_pass"}}
}
```

Kinds: `keychain` (a generic password; add `"keychain": "<path>"` to read a specific,
locked keychain file), `keychain_identity` (a cert identity as combined PEM), `file`
(one path, or several concatenated), `vault` (one field of a Vault secret, read
*through the proxy's own vault route*, so bootstrap needs no Vault token of its own)
and `device_authorize` (the OAuth2 device-code flow, cached after the first consent).

There is deliberately **no shell/command source**. This file is writable by anything
running as you, so a command source would be arbitrary code execution as you at the
next bootstrap — the same-uid problem that attended slots exist to contain, arriving
through a door they do not cover. Because the proxy never touches the Keychain itself,
it still works headless and as a separate user.

## Attended slots (high-privilege secrets)

Everything above is designed to run unattended: bootstrap once, and any process
running as you can use the routes until restart. For secrets where that is exactly
what you *don't* want — admin tokens, signing keys — mark the slot **attended**:

```json
"attended_slots": {"vault-admin": {"ttl": 300, "max_uses": 1}}
```

An attended slot is skipped by `security-proxy-bootstrap` and its routes answer
**403** (not 503 — a different fault with a different fix) until you run:

```bash
security-proxy-unlock vault-admin        # prompts; then 300s / 1 request
security-proxy-unlock vault-admin --lock # close the window early
```

The source for an attended slot lives under `"attended"` in the bootstrap config and
must point at a **locked** keychain — that is what makes the unlock cost a password
or Touch ID prompt:

```json
{"slots": {"nomad": {"keychain": {"service": "security-proxy", "account": "alinomad.cern.ch"}}},
 "attended": {"vault-admin": {"keychain": {
    "service": "vault-admin", "account": "me",
    "keychain": "~/Library/Keychains/security-proxy.keychain-db"}}}}
```

Create that keychain with its own password and a short auto-lock, and keep it out of
the default search list so nothing else touches it:

```bash
security create-keychain -P security-proxy.keychain
security set-keychain-settings -l -u -t 120 ~/Library/Keychains/security-proxy.keychain-db
./add-keychain-secret.sh -s vault-admin -k ~/Library/Keychains/security-proxy.keychain-db
```

`add-keychain-secret.sh` passes the subcommand to `security -i` on stdin, so the
secret never reaches argv (where `ps` would show it) — the shell counterpart of
`keychain_set_token()` in `security_proxy.py`. Add `-p 'Bearer '` when the slot
backs an `inject_headers` entry, which needs the full header value.

Why this shape: an agent (or any process) running as your uid can invoke
`security-proxy-unlock` exactly as you can — same user, same sockets, so no file
mode or socket permission can tell you apart. What it cannot do is answer the
Keychain prompt. The secret is therefore *absent* from the proxy outside a window a
human opened deliberately, and the proxy wipes it again after `ttl` seconds or
`max_uses` requests, whichever comes first (a sweeper drops it on time even if
nobody uses it). Setting neither `ttl` nor `max_uses` is rejected: a window that
never closes is not attended.

The gate is only as strong as the prompt — a GUI password dialog is in principle
spoofable by other code running as you. A Secure Enclave helper (a slot source that
signs a challenge under `.biometryCurrentSet`) would close that gap; the
`security-proxy-unlock` front door is unchanged if you swap one in later.

## S3 buckets (s3cmd)

S3 upstreams authenticate by *signing* each request, not by sending a bearer token,
so an S3 route holds the real access/secret keys and re-signs on the way through
(SigV4, `UNSIGNED-PAYLOAD`, path-style). The client sends an effectively-unsigned
request whose access-key-id is the gate token; the proxy validates that, then signs
with the real keys. The region is reused from what the client signed with.

Route (one keypair, or per-bucket keypairs selected by the first path segment — a
flat `access_key`/`secret_key`, if given too, is the default for buckets without an
entry):

```json
{"name": "s3", "upstream": "https://s3.cern.ch",
 "s3": {"buckets": {
   "alibuild-repo":   {"access_key": {"ingest": "s3-access-alibuild-repo"},
                       "secret_key": {"ingest": "s3-secret-alibuild-repo"}}}}}
```

Bootstrap slots point each keypair at its source (convention: `s3-access-<bucket>` /
`s3-secret-<bucket>`):

```json
{"s3-access-alibuild-repo": {"keychain": {"service": "access_key alibuild-repo.s3.cern.ch", "account": "alibuild"}},
 "s3-secret-alibuild-repo": {"keychain": {"service": "secret_key alibuild-repo.s3.cern.ch", "account": "alibuild"}}}
```

**Client side:** `s3cmd` cannot resolve the proxy from `~/.s3cfg` — its `%(...)` is
ConfigParser interpolation, not shell command substitution, so it can't run
`security-proxy-token`. And the port is random while the gate token rotates, so
neither can be static. Inject them at call time with a shell wrapper instead; keep
`~/.s3cfg` minimal (`signature_v2 = False`):

```sh
s3cmd() {
  local sock=/usr/local/var/run/security-proxy/agent.sock hp tok
  hp=$(security-proxy-token --socket "$sock" --hostport) || return 1
  tok=$(security-proxy-token --socket "$sock" s3)
  command s3cmd --no-ssl --host="$hp" --host-bucket="$hp" \
    --access_key="$tok" --secret_key=ignored-proxy-signs "$@"
}
```

`--host-bucket` equal to `--host` (no bucket in it) selects path-style addressing, so
one wrapper serves every bucket: `s3cmd ls s3://<bucket>/`. `secret_key` only has to
be non-empty (the proxy signs; its value is ignored).

## Email (mbsync / msmtp via OAuth2)

Mail clients that speak XOAUTH2 (mbsync/isync for IMAP, msmtp for SMTP) need a
short-lived OAuth2 access token as their password. The durable secrets — the OAuth
`client_secret` (if any) and the long-lived `refresh_token` — should never live on the
client. An `oauth` route holds them and brokers the refresh: the proxy performs the
`refresh_token` grant and returns only the ~1h access token, stripping any rotated
refresh token from the reply so the client never sees a long-lived credential.

Unlike the S3 route, the proxy is *not* in the mail data path — mbsync still connects
straight to the IMAP server over TLS. The proxy only brokers the token, so the access
token is necessarily handed to the client (it authenticates to IMAP itself); what the
proxy protects is the refresh token behind it.

Route — one route serves several accounts, selected by the request path. `client_secret`
is optional (public device-flow clients, e.g. the Thunderbird client id, have none):

```json
{"name": "mail", "upstream": "https://login.microsoftonline.com", "oauth": {"accounts": {
   "me@example.com": {
     "endpoint": "https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token",
     "client_id": "<public-client-id>",
     "scope": "https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send offline_access",
     "refresh_token": {"ingest": "mail-refresh"},
     "refresh_store": "/usr/local/var/lib/security-proxy/mail-me.refresh.json"}}}}
```

`refresh_store` (a path the daemon owns) is where a *rotated* refresh token is persisted
so refreshes survive restarts — needed for providers that rotate on every use
(Microsoft); harmless for those that don't (Google). A confidential client adds a
`"client_secret": {"ingest": "mail-secret"}` slot.

Get the first refresh token at provisioning time — no separate tool — with a `command`
bootstrap source that runs the device-code flow. It prompts once (on your terminal,
during `security-proxy-bootstrap`), caches the result, and emits the refresh token on
stdout:

```json
{"mail-refresh": {"device_authorize": {"provider": "microsoft", "tenant": "<tenant>",
  "client_id": "<public-client-id>", "account": "me@example.com",
  "scope": "https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send offline_access"}}}
```

**Client side.** A tiny helper prints a fresh access token; point each mail client's
password command at it (one token, scoped for both IMAP and SMTP, serves both):

```
# ~/.mbsyncrc
Host      outlook.office365.com
Port      993
SSLType   IMAPS
AuthMechs XOAUTH2
User      me@example.com
PassCmd   "/usr/local/lib/security-proxy/venv/bin/security-proxy mail-token me@example.com --socket /usr/local/var/run/security-proxy/agent.sock"

# ~/.msmtprc
auth          xoauth2
user          me@example.com
passwordeval  /usr/local/lib/security-proxy/venv/bin/security-proxy mail-token me@example.com --socket /usr/local/var/run/security-proxy/agent.sock
```

`mail-token` and `device-authorize` are subcommands of the `security-proxy` program
(not separate `security-proxy-*` helpers), and it lives in the venv `bin/`, which is
usually not on `PATH` — and `PassCmd`/`passwordeval`/`command` sources run under
`/bin/sh` anyway. So always invoke them by **absolute path**
(`/usr/local/lib/security-proxy/venv/bin/security-proxy …`). mbsync also needs XOAUTH2
SASL support (`cyrus-sasl-xoauth2`). `mail-token` exits non-zero with nothing on stdout
on failure, so the client treats it as an auth error.

## Running

```bash
./venv/bin/security-proxy --config ~/.security-proxy.json
```

The port is random and the gate token rotates daily, so point clients at the proxy
through the helper:

```bash
export NOMAD_ADDR=$(security-proxy-token --addr)
export NOMAD_TOKEN=$(security-proxy-token nomad)
```

`security-proxy-token <service>` prints that service's current token;
`--addr`/`--port` print the connection info; `--json` prints the raw reply.
WebSocket clients that cannot send `Authorization` can authenticate with
`Sec-WebSocket-Protocol: security-proxy-token.<token>[, real-protocol]`; the
proxy strips the reserved token entry before forwarding subprotocols upstream.

## Credential sources

The grid certificate can come from PEM files (`cert`+`key`), a PKCS12 file
(`p12`), or the macOS Keychain (`keychain_identity`). The Keychain option exports
the matching identity name or SHA-1 fingerprint to
`~/.globus/security-proxy-keychain.p12` at startup (0600), and fails if the
selector is ambiguous.

## Deploy with launchctl on macOS

```bash
sed "s|__HOME__|$HOME|g" ch.cern.security-proxy.plist \
  > ~/Library/LaunchAgents/ch.cern.security-proxy.plist
launchctl load ~/Library/LaunchAgents/ch.cern.security-proxy.plist
```

Management commands:

```bash
launchctl start ch.cern.security-proxy
launchctl stop  ch.cern.security-proxy
launchctl unload ~/Library/LaunchAgents/ch.cern.security-proxy.plist
launchctl list | grep security-proxy
tail -f /tmp/security-proxy.stdout.log /tmp/security-proxy.stderr.log
```

## Run as a separate user (LaunchDaemon)

Because the proxy never reads secrets itself, it can run as a dedicated user that
holds nothing your interactive account can reach. Set `agent_socket_group` and
`ingest_socket_group` in the config so token clients and secret provisioners can
be controlled independently.

Files for this layout: `ch.cern.security-proxy.daemon.plist` and
`config.daemon.sample.json`.

```sh
# 1. dedicated daemon user plus separate token/provisioning groups
#    (pick free IDs; check `dscl . list /Users UniqueID`)
sudo dscl . create /Groups/_securityproxy PrimaryGroupID 450
sudo dscl . create /Groups/_securityproxy_clients PrimaryGroupID 451
sudo dscl . create /Groups/_securityproxy_provisioners PrimaryGroupID 452
sudo dscl . create /Users/_securityproxy UniqueID 450 PrimaryGroupID 450 \
     UserShell /usr/bin/false NFSHomeDirectory /var/empty IsHidden 1
sudo dscl . append /Groups/_securityproxy_clients GroupMembership _securityproxy
sudo dscl . append /Groups/_securityproxy_provisioners GroupMembership _securityproxy
sudo dscl . append /Groups/_securityproxy_clients GroupMembership "$USER"
sudo dscl . append /Groups/_securityproxy_provisioners GroupMembership "$USER"  # for bootstrap

# 2. deploy code + venv where the daemon user can read (NOT your home, which is 0700)
sudo mkdir -p /usr/local/lib/security-proxy
sudo cp security_proxy.py pyproject.toml /usr/local/lib/security-proxy/
sudo python3 -m venv /usr/local/lib/security-proxy/venv
sudo /usr/local/lib/security-proxy/venv/bin/pip install -e /usr/local/lib/security-proxy

# 3. config + grid cert the daemon owns (its own copy; re-copy on cert renewal)
sudo mkdir -p /usr/local/etc/security-proxy \
             /usr/local/var/run/security-proxy/agent \
             /usr/local/var/run/security-proxy/ingest \
             /usr/local/var/log
sudo cp config.daemon.sample.json /usr/local/etc/security-proxy/config.json
sudo cp ~/.globus/usercert.pem ~/.globus/userkey.pem ~/.globus/cern-ca-bundle.pem \
        /usr/local/etc/security-proxy/
sudo chown root:wheel /usr/local/etc/security-proxy/config.json
sudo chown _securityproxy:_securityproxy \
     /usr/local/etc/security-proxy/usercert.pem \
     /usr/local/etc/security-proxy/userkey.pem \
     /usr/local/etc/security-proxy/cern-ca-bundle.pem
sudo chown _securityproxy:_securityproxy_clients /usr/local/var/run/security-proxy/agent
sudo chown _securityproxy:_securityproxy_provisioners /usr/local/var/run/security-proxy/ingest
sudo chmod 2750 /usr/local/var/run/security-proxy/agent /usr/local/var/run/security-proxy/ingest
sudo chmod 600 /usr/local/etc/security-proxy/userkey.pem

# 4. load the daemon (runs at boot, before login)
sudo cp ch.cern.security-proxy.daemon.plist /Library/LaunchDaemons/ch.cern.security-proxy.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/ch.cern.security-proxy.plist
```

Then your shell/bootstrap target the role-specific sockets (you read the Keychain
and push across the ingest socket; the daemon never touches a Keychain):

```sh
A=/usr/local/var/run/security-proxy/agent/agent.sock
I=/usr/local/var/run/security-proxy/ingest/ingest.sock
export NOMAD_ADDR=$(security-proxy-token --socket $A --addr)
export NOMAD_TOKEN=$(security-proxy-token --socket $A nomad)
security-proxy-bootstrap --socket $I
```

To hold **no** secret files on the daemon at all, push the grid certificate in too:
in the config set `"cert": {"ingest": "grid-cert"}` (and drop the `cert`/`key` file
paths), then add a bootstrap slot for it. Either export it from the Keychain:

```json
{"grid-cert": {"keychain_identity": "My Grid Cert"}}
```

or, if your cert lives as PEM files, `{"grid-cert": {"file": ["~/.globus/usercert.pem", "~/.globus/userkey.pem"]}}`.

mTLS routes return `503` until it is pushed; the key is only ever briefly
materialised to a `0600` temp file while loading, never persisted.

### Redeploy / restart

The steps above are automated (and idempotent) by
[`setup-separate-user.sh`](setup-separate-user.sh) — re-run it to pick up new code
or config:

```sh
sudo bash setup-separate-user.sh     # installs code + config, then restarts in place
security-proxy-bootstrap --socket /usr/local/var/run/security-proxy/ingest/ingest.sock
```

The script restarts the daemon for you with `launchctl kickstart -k
system/ch.cern.security-proxy` — a hard restart in place. **Do not** `stop`/`kill`
it manually first: with `KeepAlive` set, `launchd` would respawn the *old* binary
before the new one is installed, and a manual `bootout`/`bootstrap` cycle can race
into `Bootstrap failed: 5: Input/output error`. Let the script (or `kickstart`) own
the restart.

The setup script installs Python dependencies from `requirements.lock` with
`--require-hashes`; regenerate that lock when changing dependencies, Python, or
platform.

To regenerate the dependency lock from `pyproject.toml`:

```sh
cd security-proxy
python3 -m venv /tmp/security-proxy-lock
/tmp/security-proxy-lock/bin/pip install --upgrade pip
/tmp/security-proxy-lock/bin/pip install pip-tools
/tmp/security-proxy-lock/bin/pip-compile \
  --generate-hashes \
  --only-binary=:all: \
  --output-file requirements.lock \
  pyproject.toml
rm -rf /tmp/security-proxy-lock
```

Then verify the locked install path:

```sh
python3 -m venv /tmp/security-proxy-test
/tmp/security-proxy-test/bin/pip install --require-hashes -r requirements.lock
/tmp/security-proxy-test/bin/pip install --no-deps --no-build-isolation -e .
/tmp/security-proxy-test/bin/security-proxy --help
rm -rf /tmp/security-proxy-test
```

Every restart wipes the in-memory slots, so **always re-run `security-proxy-bootstrap`
afterwards** (grid cert, service tokens, S3 keys, …); mTLS/injected/S3 routes return
`503` until you do. To restart without redeploying:

```sh
sudo launchctl kickstart -k system/ch.cern.security-proxy
security-proxy-bootstrap
```

## Deploying on Linux (systemd)

[`setup-separate-user-linux.sh`](setup-separate-user-linux.sh) is the Linux counterpart
of `setup-separate-user.sh`. It is deliberately a **separate script**: the two service
managers and directory systems share almost nothing, and a single branching script that
runs as root would be much harder to audit.

```sh
sudo bash setup-separate-user-linux.sh
```

Same trust model and same socket privilege split; the mechanics differ:

| | macOS | Linux |
|---|---|---|
| users/groups | `dscl` | `groupadd` / `useradd --system` / `usermod -aG` |
| service manager | LaunchDaemon plist + `launchctl` | systemd unit + `systemctl` |
| logs | `/usr/local/var/log/*.log` (0600) | the journal (`journalctl -u security-proxy`) |
| config | `/usr/local/etc/security-proxy` | `/etc/security-proxy` |
| runtime sockets | `/usr/local/var/run/security-proxy` | `/run/security-proxy` (tmpfs) |
| state | `/usr/local/var/lib/security-proxy` | `/var/lib/security-proxy` |
| mode/owner checks | BSD `stat -f` | GNU `stat -c` |

Linux-specific points worth knowing:

- **`/run` is tmpfs**, so the socket directories vanish on reboot. The script installs
  `/etc/tmpfiles.d/security-proxy.conf` so systemd recreates them — with the right
  owner/group **and the setgid bit** — on every boot.
- **The setgid bit actually matters here.** On BSD a new file inherits its parent
  directory's group regardless; on Linux that inheritance *requires* setgid. It is what
  keeps the agent socket in the client group and the ingest socket in the provisioner
  group. Any `chown` after a `chmod` silently clears it (for a non-root caller), so both
  the script and the daemon always chown first and chmod last.
- **No Keychain.** Every bootstrap slot must use a `command` source — `pass show …`,
  `secret-tool lookup …`, `op read …`, or a file. The `keychain` /`keychain_identity`
  source types are macOS-only.
- **The dependency lock is platform + interpreter specific.** `requirements.lock` is
  built for macOS/CPython and will *not* verify on Linux; generate a
  `requirements-linux.lock` on the target host (the script prints the exact command, and
  accepts `SECURITY_PROXY_LOCK=/path/to/lock`).
- The unit is sandboxed (`ProtectSystem=strict`, `ProtectHome`, `NoNewPrivileges`,
  empty `CapabilityBoundingSet`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`),
  with `ReadWritePaths` limited to the runtime and state directories.

Restart and re-provision:

```sh
sudo systemctl restart security-proxy
security-proxy-bootstrap --socket /run/security-proxy/ingest/ingest.sock
journalctl -u security-proxy -f
```
