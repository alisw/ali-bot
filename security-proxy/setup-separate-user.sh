#!/usr/bin/env bash
# Set up security-proxy as a dedicated, shell-less user (_securityproxy) running as a
# LaunchDaemon. REVIEW THIS FIRST, then run it yourself:
#
#     sudo bash setup-separate-user.sh
#
# It does NOT touch your private key or your Keychain. The grid cert and the Nomad
# token are NOT placed on disk -- after this runs, you provision them at runtime with
# `security-proxy-bootstrap` (which reads them as *you*, and pushes them to the daemon).
# Safe to re-run. Config is installed root-owned: change it later with sudo + restart.
set -euo pipefail

PROXY_USER=_securityproxy
CLIENT_GROUP=_securityproxy_clients
PROVISION_GROUP=_securityproxy_provisioners
PROXY_ID_DEFAULT=484                               # first-run id (auto-bumped if taken)
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"          # repo dir holding security_proxy.py
ADMIN_USER="${SUDO_USER:-$USER}"                  # the human, added to client/provision groups
LIB=/usr/local/lib/security-proxy
ETC=/usr/local/etc/security-proxy
RUN=/usr/local/var/run/security-proxy
AGENT_RUN=$RUN/agent
INGEST_RUN=$RUN/ingest
STATE=/usr/local/var/lib/security-proxy            # daemon-writable: oauth refresh_store
LOG=/usr/local/var/log
CAFILE_SRC="/Users/${ADMIN_USER}/.globus/cern-ca-bundle.pem"
PLIST=/Library/LaunchDaemons/ch.cern.security-proxy.plist

[ "$(id -u)" = 0 ] || { echo "must run as root: sudo bash $0" >&2; exit 1; }

CONFIG_TMP=
PLIST_TMP=
_cleanup_tmp() {
  [ -z "${CONFIG_TMP:-}" ] || rm -f "$CONFIG_TMP"
  [ -z "${PLIST_TMP:-}" ] || rm -f "$PLIST_TMP"
}
trap _cleanup_tmp EXIT

fail() { echo "ERROR: $*" >&2; exit 1; }

_reject_symlink_path() {
  _path=$1
  case "$_path" in
    /*) ;;
    *) fail "path must be absolute: $_path" ;;
  esac
  _cur=
  IFS=/ read -r -a _parts <<< "${_path#/}"
  for _part in "${_parts[@]}"; do
    [ -n "$_part" ] || continue
    _cur="${_cur}/$_part"
    # NB: `[ -L x ] && fail` would make the loop (and this function) return 1 in the
    # normal not-a-symlink case, which under `set -e` silently aborts the caller.
    if [ -L "$_cur" ]; then
      fail "refusing symlink path component: $_cur"
    fi
  done
  return 0
}

_reject_writable_parent_chain() {
  _path=$1
  _dir=$(dirname "$_path")
  _cur=
  IFS=/ read -r -a _parts <<< "${_dir#/}"
  for _part in "${_parts[@]}"; do
    [ -n "$_part" ] || continue
    _cur="${_cur}/$_part"
    [ -e "$_cur" ] || break
    [ -d "$_cur" ] || fail "parent path is not a directory: $_cur"
    if [ -L "$_cur" ]; then
      fail "refusing symlink parent: $_cur"
    fi
    _mode=$(stat -f '%Lp' "$_cur")
    if (( (8#$_mode & 0022) != 0 )); then
      fail "refusing group/world-writable parent directory: $_cur (mode $_mode)"
    fi
  done
  return 0
}

_require_regular_or_absent() {
  _path=$1
  _reject_symlink_path "$_path"
  [ ! -e "$_path" ] || [ -f "$_path" ] || fail "refusing non-regular file: $_path"
}

_assert_path() {
  _path=$1
  _type=$2
  _owner=$3
  _group=$4
  _mode=$5
  _reject_symlink_path "$_path"
  case "$_type" in
    dir) [ -d "$_path" ] || fail "expected directory: $_path" ;;
    file) [ -f "$_path" ] || fail "expected file: $_path" ;;
    *) fail "internal error: unknown path type $_type" ;;
  esac
  _actual_owner=$(stat -f '%Su' "$_path")
  _actual_group=$(stat -f '%Sg' "$_path")
  # %Lp is only the low 3 octal digits -- it drops setuid/setgid/sticky, so a setgid
  # dir (2750) would read back as "750". %Mp supplies the special-bits digit, so
  # %Mp%Lp gives the full 4-digit mode ("2750", "0755"); expected values are 4-digit.
  _actual_mode=$(stat -f '%Mp%Lp' "$_path")
  [ "$_actual_owner" = "$_owner" ] || fail "$_path owner is $_actual_owner, expected $_owner"
  [ "$_actual_group" = "$_group" ] || fail "$_path group is $_actual_group, expected $_group"
  [ "$_actual_mode" = "$_mode" ] || fail "$_path mode is $_actual_mode, expected $_mode"
}

_assert_not_group_world_writable_dir() {
  _path=$1
  _reject_symlink_path "$_path"
  [ -d "$_path" ] || fail "expected directory: $_path"
  _mode=$(stat -f '%Lp' "$_path")
  if (( (8#$_mode & 0022) != 0 )); then
    fail "refusing group/world-writable directory: $_path (mode $_mode)"
  fi
}

_atomic_replace() {
  _tmp=$1
  _target=$2
  _owner=$3
  _group=$4
  _mode=$5
  _require_regular_or_absent "$_target"
  chown "$_owner:$_group" "$_tmp"
  chmod "$_mode" "$_tmp"
  mv -f "$_tmp" "$_target"
  _assert_path "$_target" file "$_owner" "$_group" "$_mode"
}

# Resolve the uid/gid idempotently: reuse the existing account's values on re-runs,
# otherwise pick the first free id at/above the default (never hardcode/clobber).
_id_in_use() {  # is $1 used as any user UID or group GID?
  { dscl . -list /Users UniqueID; dscl . -list /Groups PrimaryGroupID; } \
    | awk '{print $2}' | grep -qx "$1"
}
_next_free_id() {
  _id=$1
  while _id_in_use "$_id"; do _id=$((_id + 1)); done
  echo "$_id"
}
_ensure_group() {
  _group=$1
  _default_id=$2
  _gid=$(dscl . -read /Groups/"$_group" PrimaryGroupID 2>/dev/null | awk '{print $2}') || true
  if [ -z "$_gid" ]; then
    _gid=$(_next_free_id "$_default_id")
    dscl . -create /Groups/"$_group"
    dscl . -create /Groups/"$_group" PrimaryGroupID "$_gid"
    dscl . -create /Groups/"$_group" RealName "Security Proxy $(echo "$_group" | sed 's/^_securityproxy_//')"
  fi
  echo "$_gid"
}
PROXY_GID=$(dscl . -read /Groups/$PROXY_USER PrimaryGroupID 2>/dev/null | awk '{print $2}') || true
PROXY_UID=$(dscl . -read /Users/$PROXY_USER UniqueID 2>/dev/null | awk '{print $2}') || true
if [ -z "$PROXY_GID" ] || [ -z "$PROXY_UID" ]; then
  _id=$(_next_free_id "$PROXY_ID_DEFAULT")
  : "${PROXY_GID:=$_id}"
  : "${PROXY_UID:=$_id}"
fi

echo "==> dedicated group + user ($PROXY_USER, uid=$PROXY_UID gid=$PROXY_GID)"
if ! dscl . -read /Groups/$PROXY_USER >/dev/null 2>&1; then
  dscl . -create /Groups/$PROXY_USER
  dscl . -create /Groups/$PROXY_USER PrimaryGroupID $PROXY_GID
  dscl . -create /Groups/$PROXY_USER RealName "Security Proxy"
fi
if ! dscl . -read /Users/$PROXY_USER >/dev/null 2>&1; then
  dscl . -create /Users/$PROXY_USER
  dscl . -create /Users/$PROXY_USER UniqueID $PROXY_UID
  dscl . -create /Users/$PROXY_USER PrimaryGroupID $PROXY_GID
  dscl . -create /Users/$PROXY_USER UserShell /usr/bin/false
  dscl . -create /Users/$PROXY_USER NFSHomeDirectory /var/empty
  dscl . -create /Users/$PROXY_USER RealName "Security Proxy"
  dscl . -create /Users/$PROXY_USER IsHidden 1
fi

echo "==> socket access groups"
CLIENT_GID=$(_ensure_group "$CLIENT_GROUP" $((PROXY_ID_DEFAULT + 1)))
PROVISION_GID=$(_ensure_group "$PROVISION_GROUP" $((PROXY_ID_DEFAULT + 2)))
# The daemon creates the sockets; the human fetches tokens and provisions secrets.
dscl . -append /Groups/$CLIENT_GROUP GroupMembership "$PROXY_USER" 2>/dev/null || true
dscl . -append /Groups/$PROVISION_GROUP GroupMembership "$PROXY_USER" 2>/dev/null || true
dscl . -append /Groups/$CLIENT_GROUP GroupMembership "$ADMIN_USER" 2>/dev/null || true
dscl . -append /Groups/$PROVISION_GROUP GroupMembership "$ADMIN_USER" 2>/dev/null || true

echo "==> directories"
for _path in "$LIB" "$ETC" "$RUN" "$AGENT_RUN" "$INGEST_RUN" "$STATE" "$LOG" \
             "$ETC/config.json" "$PLIST" \
             "$LOG/security-proxy.out.log" "$LOG/security-proxy.err.log"; do
  _reject_symlink_path "$_path"
  _reject_writable_parent_chain "$_path"
done
install -d -o root -g wheel -m 0755 "$LIB" "$ETC" "$RUN"
mkdir -p "$LOG"
install -d -o "$PROXY_USER" -g "$CLIENT_GROUP" -m 02750 "$AGENT_RUN"
install -d -o "$PROXY_USER" -g "$PROVISION_GROUP" -m 02750 "$INGEST_RUN"
install -d -o "$PROXY_USER" -g "$PROXY_USER" -m 0700 "$STATE"
_assert_path "$LIB" dir root wheel 0755
_assert_path "$ETC" dir root wheel 0755
_assert_path "$RUN" dir root wheel 0755
_assert_not_group_world_writable_dir "$LOG"
_assert_path "$AGENT_RUN" dir "$PROXY_USER" "$CLIENT_GROUP" 2750
_assert_path "$INGEST_RUN" dir "$PROXY_USER" "$PROVISION_GROUP" 2750
_assert_path "$STATE" dir "$PROXY_USER" "$PROXY_USER" 0700

echo "==> code + venv (deps from hash-checked lock)"
for _path in "$LIB/security_proxy.py" "$LIB/pyproject.toml" "$LIB/requirements.lock" \
             "$ETC/cern-ca-bundle.pem"; do
  _require_regular_or_absent "$_path"
done
install -m 0644 "$SRC_DIR/security_proxy.py" "$LIB/security_proxy.py"
install -m 0644 "$SRC_DIR/pyproject.toml"     "$LIB/pyproject.toml"
install -m 0644 "$SRC_DIR/requirements.lock"  "$LIB/requirements.lock"
# Pin the interpreter to the one the lock was generated for. The lock carries ONE
# wheel hash per package, and cffi/cryptography ship per-Python wheels, so a
# different minor version downloads a different file and --require-hashes fails:
#
#   Expected sha256 c654de54...  (cffi-2.0.0-cp314-cp314-macosx_11_0_arm64.whl)
#        Got        45d5e886...  (cffi-2.0.0-cp313-cp313-macosx_11_0_arm64.whl)
#
# which is exactly what an active virtualenv on PATH produces -- sudo does not
# reset PATH, so whichever python3 the caller happened to have wins. Ask for the
# version by name instead of trusting PATH, and say so plainly when it is missing,
# rather than failing later inside pip with a mismatch that reads like tampering.
_lock_py=$(sed -n 's/^# python-requires: \([0-9][0-9.]*\).*$/\1/p' "$LIB/requirements.lock")
: "${_lock_py:?requirements.lock has no '# python-requires:' line}"
PYTHON=${SECURITY_PROXY_PYTHON:-}
if [ -z "$PYTHON" ]; then
  if command -v "python$_lock_py" >/dev/null 2>&1; then PYTHON=python$_lock_py; else PYTHON=python3; fi
fi
_have_py=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
if [ "$_have_py" != "$_lock_py" ]; then
  echo "ERROR: requirements.lock is pinned to CPython $_lock_py, but '$PYTHON' is ${_have_py:-unusable}." >&2
  echo "       Install python$_lock_py, or point SECURITY_PROXY_PYTHON at it:" >&2
  echo "         sudo SECURITY_PROXY_PYTHON=/opt/homebrew/bin/python$_lock_py bash $0" >&2
  echo "       (An active virtualenv on PATH is the usual cause: sudo keeps it.)" >&2
  exit 1
fi
# --clear: rebuild rather than reuse, because reuse has been observed to leave a
# venv that is internally inconsistent. After one run with a 3.13 interpreter on
# PATH, the deployed venv had pyvenv.cfg and bin/python saying 3.14, a
# lib/python3.13 AND a lib/python3.14, and a bin/pip whose shebang pointed at
# 3.13. The next run therefore installed with a 3.13 pip, which fetches cp313
# wheels and fails --require-hashes against a lock generated for 3.14 -- and it
# kept failing that way even once PATH had been corrected.
#
# Exactly which of venv's steps it skips on an existing directory is not worth
# depending on either way: --clear removes the question. It costs nothing here,
# since the installs below are --no-cache-dir and refetch regardless.
"$PYTHON" -m venv --clear "$LIB/venv"
"$LIB/venv/bin/pip" install -q --disable-pip-version-check --no-cache-dir \
  --require-hashes -r "$LIB/requirements.lock"
"$LIB/venv/bin/pip" install -q --disable-pip-version-check --no-cache-dir \
  --no-deps --no-build-isolation -e "$LIB"
chown -R -h root:wheel "$LIB"
find "$LIB" ! -type l -exec chmod go-w {} +
_assert_path "$LIB/security_proxy.py" file root wheel 0644
_assert_path "$LIB/pyproject.toml" file root wheel 0644
_assert_path "$LIB/requirements.lock" file root wheel 0644

echo "==> CA bundle (non-secret) copied so the daemon user can read it"
install -m 0644 "$CAFILE_SRC" "$ETC/cern-ca-bundle.pem"
_assert_path "$ETC/cern-ca-bundle.pem" file root wheel 0644

echo "==> config (root-owned; cert+secrets are pushed at runtime, not stored here)"
# "attended_slots" lists slots that bootstrap must NOT fill: high-privilege secrets
# that only enter the proxy when you run `security-proxy-unlock <slot>` and answer a
# Keychain password / Touch ID prompt, and that the proxy then drops after the window
# below. Add an entry per privileged slot, e.g.
#     "attended_slots": {"vault-admin": {"ttl": 300, "max_uses": 1}},
# and a matching source under "attended" in ~/.security-proxy-bootstrap.json pointing
# at a *locked* keychain (an unlocked one would defeat the prompt).
#
# "ccdb" and "ccdb-prod" deliberately share an upstream HERE, and only here. In
# CI (ci-jobs/ci-linux-x86.nomad) they are different servers: "ccdb" fronts the
# writable ccdb-test, because the O2 CCDB suites upload, while "ccdb-prod"
# fronts production for the tests pinned to objects that exist only there. This
# host has no ccdb-test route at all, so the second name exists purely so that
# anything written against the CI shape -- O2's ALICEO2_CCDB_PRODUCTION_HOST and
# the ALICEO2_CCDB_AUTH_TOKENS table -- resolves locally instead of falling
# through to "no route matches this path". Route names are what gate tokens are
# minted from, so the two get DIFFERENT tokens even pointing at one upstream.
CONFIG_TMP=$(mktemp "$ETC/config.json.XXXXXX")
cat > "$CONFIG_TMP" <<JSON
{
  "cafile": "/usr/local/etc/security-proxy/cern-ca-bundle.pem",
  "cert": {"ingest": "grid-cert"},
  "agent_socket": "$AGENT_RUN/agent.sock",
  "ingest_socket": "$INGEST_RUN/ingest.sock",
  "agent_socket_group": "$CLIENT_GROUP",
  "ingest_socket_group": "$PROVISION_GROUP",
  "secret_rotation_seconds": 86400,
  "attended_slots": {"github-token-rw": {"ttl": 900}, "nomad-rw": {"ttl": 900},
                     "nomad-admin": {"ttl": 300, "max_uses": 5}},
  "routes": [
    {"prefix": "/ccdb/", "upstream": "https://alice-ccdb.cern.ch"},
    {"name": "ccdb-prod", "prefix": "/ccdb-prod/", "upstream": "https://alice-ccdb.cern.ch"},
    {"prefix": "/hyperloop/", "upstream": "https://alimonitor.cern.ch/hyperloop"},
    {"prefix": "/remote-mcp/", "upstream": "wss://alien.cern.ch", "websocket": true},
    {"prefix": "/alimonitor/", "upstream": "https://alimonitor.cern.ch"},
    {"prefix": "/alihyperloop-data/", "upstream": "https://alimonitor.cern.ch/alihyperloop-data"},
    {"prefix": "/bookkeeping/", "upstream": "https://ali-bookkeeping.cern.ch"},
    {"name": "github", "prefix": "/github/", "upstream": "https://api.github.com",
     "inject_headers": {"Authorization": {"ingest": "github-token"}}},
    {"name": "github-rw", "prefix": "/github-rw/", "upstream": "https://api.github.com",
     "inject_headers": {"Authorization": {"ingest": "github-token-rw"}}},
    {"name": "nomad", "upstream": "https://alinomad.cern.ch", "websocket": true,
     "auth_header": "X-Nomad-Token", "inject_headers": {"X-Nomad-Token": {"ingest": "nomad"}}},
    {"name": "nomad-rw", "upstream": "https://alinomad.cern.ch", "websocket": true,
     "auth_header": "X-Nomad-Token", "inject_headers": {"X-Nomad-Token": {"ingest": "nomad-rw"}}},
    {"name": "nomad-admin", "upstream": "https://alinomad.cern.ch", "websocket": true,
     "auth_header": "X-Nomad-Token", "inject_headers": {"X-Nomad-Token": {"ingest": "nomad-admin"}}},
    {"name": "consul", "upstream": "https://aliconsul.cern.ch",
     "auth_header": "X-Consul-Token", "inject_headers": {"X-Consul-Token": {"ingest": "consul"}}},
    {"name": "vault", "upstream": "https://alivault.cern.ch",
     "auth_header": "X-Vault-Token", "inject_headers": {"X-Vault-Token": {"ingest": "vault"}}},
    {"name": "s3", "upstream": "https://s3.cern.ch",
     "s3": {"access_key": {"ingest": "s3-access"}, "secret_key": {"ingest": "s3-secret"}}},
    {"name": "mail", "upstream": "https://login.microsoftonline.com", "oauth": {"accounts": {
       "giulio.eulisse@cern.ch": {
         "endpoint": "https://login.microsoftonline.com/c80d3499-4a40-4a8c-986e-abce017d6b19/oauth2/v2.0/token",
         "client_id": "9e5f94bc-e8a4-4e73-b8be-63364c29d753",
         "scope": "https://outlook.office.com/IMAP.AccessAsUser.All https://outlook.office.com/SMTP.Send offline_access",
         "refresh_token": {"ingest": "mail-refresh"},
         "refresh_store": "/usr/local/var/lib/security-proxy/mail-giulio.refresh.json"}}}},
    {"name": "alibuild-ac-sign", "prefix": "/sign/alibuild-ac",
     "sign": {"key": {"ingest": "alibuild-ac-sign-key"}}},
    {"name": "mimir", "prefix": "/mimir/",
     "upstream": "https://monit-grafana.cern.ch/api/datasources",
     "inject_headers": {"Authorization": {"ingest": "mimir-read"}}},
    {"name": "grafana-annotate", "prefix": "/grafana/",
     "upstream": "https://monit-grafana.cern.ch",
     "inject_headers": {"Authorization": {"ingest": "grafana-annotate-token"}}}
  ]
}
JSON
_atomic_replace "$CONFIG_TMP" "$ETC/config.json" root wheel 0644
CONFIG_TMP=

echo "==> runtime + log ownership (daemon writes its sockets/logs)"
chown root:wheel "$RUN"
chmod 0755 "$RUN"
chown "$PROXY_USER:$CLIENT_GROUP" "$AGENT_RUN"
chmod 02750 "$AGENT_RUN"
chown "$PROXY_USER:$PROVISION_GROUP" "$INGEST_RUN"
chmod 02750 "$INGEST_RUN"
chown "$PROXY_USER:$PROXY_USER" "$STATE"   # daemon persists rotated oauth refresh tokens here
chmod 0700 "$STATE"
for _log_file in "$LOG/security-proxy.out.log" "$LOG/security-proxy.err.log"; do
  _require_regular_or_absent "$_log_file"
  if [ ! -e "$_log_file" ]; then
    install -o "$PROXY_USER" -g "$PROXY_USER" -m 0600 /dev/null "$_log_file"
  else
    chown "$PROXY_USER:$PROXY_USER" "$_log_file"
    chmod 0600 "$_log_file"
  fi
  _assert_path "$_log_file" file "$PROXY_USER" "$PROXY_USER" 0600
done

echo "==> LaunchDaemon"
PLIST_TMP=$(mktemp "$PLIST.XXXXXX")
cat > "$PLIST_TMP" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>ch.cern.security-proxy</string>
    <key>UserName</key><string>_securityproxy</string>
    <key>GroupName</key><string>_securityproxy</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/lib/security-proxy/venv/bin/python</string>
        <string>/usr/local/lib/security-proxy/security_proxy.py</string>
        <string>--config</string>
        <string>/usr/local/etc/security-proxy/config.json</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>Umask</key><integer>63</integer>
    <key>StandardOutPath</key><string>/usr/local/var/log/security-proxy.out.log</string>
    <key>StandardErrorPath</key><string>/usr/local/var/log/security-proxy.err.log</string>
    <key>WorkingDirectory</key><string>/usr/local/lib/security-proxy</string>
</dict>
</plist>
PLIST
_atomic_replace "$PLIST_TMP" "$PLIST" root wheel 0644
PLIST_TMP=

echo "==> (re)load the daemon"
if launchctl print system/ch.cern.security-proxy >/dev/null 2>&1; then
  # already loaded: restart in place to pick up the new code + config (avoids the
  # bootout/bootstrap race that yields "Bootstrap failed: 5: Input/output error")
  launchctl kickstart -k system/ch.cern.security-proxy
else
  launchctl bootstrap system "$PLIST"
fi

cat <<EOF

Done. The daemon is running but UNPROVISIONED (mTLS routes 503 until you push the
cert + token).

Next, as $ADMIN_USER (NOT root):
  1. Start a NEW login session so your new '$CLIENT_GROUP' and
     '$PROVISION_GROUP' group memberships apply
     (log out/in, or open a fresh Terminal window).
  2. security-proxy-bootstrap --socket $INGEST_RUN/ingest.sock
     # pushes Nomad token + grid cert into the daemon
  3. eval "\$(printf 'export NOMAD_ADDR=%s NOMAD_TOKEN=%s' \\
       "\$(security-proxy-token --socket $AGENT_RUN/agent.sock --addr)" \\
       "\$(security-proxy-token --socket $AGENT_RUN/agent.sock nomad)")"
     nomad node status
EOF
