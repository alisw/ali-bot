#!/usr/bin/env bash
# Linux counterpart of setup-separate-user.sh: run security-proxy as a dedicated,
# shell-less system user under systemd. REVIEW THIS FIRST, then run it yourself:
#
#     sudo bash setup-separate-user-linux.sh
#
# It does NOT touch your private key or any secret store. The grid cert and service
# tokens are NOT placed on disk -- after this runs you provision them at runtime with
# `security-proxy-bootstrap` (which reads them as *you*, and pushes them to the daemon).
# Safe to re-run. Config is installed root-owned: change it later with sudo + restart.
#
# Differences from the macOS script (see README "Deploying on Linux"):
#   * users/groups via groupadd/useradd instead of dscl
#   * systemd unit instead of a LaunchDaemon plist; logs go to the journal
#   * runtime dirs under /run (tmpfs) recreated at boot via systemd-tmpfiles
#   * GNU `stat -c` instead of BSD `stat -f`
#   * NO macOS Keychain: bootstrap sources must be "command" (pass/secret-tool/op/...)
set -euo pipefail

PROXY_USER=securityproxy
PROXY_GROUP=securityproxy
CLIENT_GROUP=securityproxy_clients
PROVISION_GROUP=securityproxy_provisioners
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"          # repo dir holding security_proxy.py
ADMIN_USER="${SUDO_USER:-$USER}"                  # the human, added to client/provision groups
LIB=/usr/local/lib/security-proxy
ETC=/etc/security-proxy
RUN=/run/security-proxy                            # tmpfs: recreated at boot by tmpfiles.d
AGENT_RUN=$RUN/agent
INGEST_RUN=$RUN/ingest
STATE=/var/lib/security-proxy                      # daemon-writable: oauth refresh_store
UNIT=/etc/systemd/system/security-proxy.service
TMPFILES=/etc/tmpfiles.d/security-proxy.conf
# The lock is platform+interpreter specific; the macOS one will NOT verify here.
LOCK="${SECURITY_PROXY_LOCK:-$SRC_DIR/requirements-linux.lock}"

[ "$(id -u)" = 0 ] || { echo "must run as root: sudo bash $0" >&2; exit 1; }

CONFIG_TMP=
UNIT_TMP=
TMPFILES_TMP=
_cleanup_tmp() {
  [ -z "${CONFIG_TMP:-}" ] || rm -f "$CONFIG_TMP"
  [ -z "${UNIT_TMP:-}" ] || rm -f "$UNIT_TMP"
  [ -z "${TMPFILES_TMP:-}" ] || rm -f "$TMPFILES_TMP"
}
trap _cleanup_tmp EXIT

fail() { echo "ERROR: $*" >&2; exit 1; }

# GNU stat prints modes without leading zeros ("755"), but keeps special bits when
# present ("2750"). Pad to 4 digits so comparisons are unambiguous.
_mode4() {
  _m=$1
  while [ ${#_m} -lt 4 ]; do _m="0$_m"; done
  printf '%s' "$_m"
}

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
    _mode=$(stat -c '%a' "$_cur")
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
  _actual_owner=$(stat -c '%U' "$_path")
  _actual_group=$(stat -c '%G' "$_path")
  _actual_mode=$(_mode4 "$(stat -c '%a' "$_path")")
  _want_mode=$(_mode4 "$_mode")
  [ "$_actual_owner" = "$_owner" ] || fail "$_path owner is $_actual_owner, expected $_owner"
  [ "$_actual_group" = "$_group" ] || fail "$_path group is $_actual_group, expected $_group"
  [ "$_actual_mode" = "$_want_mode" ] || fail "$_path mode is $_actual_mode, expected $_want_mode"
}

_assert_not_group_world_writable_dir() {
  _path=$1
  _reject_symlink_path "$_path"
  [ -d "$_path" ] || fail "expected directory: $_path"
  _mode=$(stat -c '%a' "$_path")
  if (( (8#$_mode & 0022) != 0 )); then
    fail "refusing group/world-writable directory: $_path (mode $_mode)"
  fi
}

# Create/fix a setgid dir. chmod goes LAST: for a non-root caller chown() clears the
# setgid bit, and on Linux that bit is what makes the socket inherit the dir's group.
_install_setgid_dir() {
  _path=$1
  _owner=$2
  _group=$3
  _mode=$4
  mkdir -p "$_path"
  chown "$_owner:$_group" "$_path"
  chmod "$_mode" "$_path"
}

echo "==> prerequisites"
command -v systemctl >/dev/null || fail "systemd (systemctl) not found; this script targets systemd hosts"
command -v useradd >/dev/null || fail "useradd not found"
stat -c '%U' / >/dev/null 2>&1 || fail "GNU stat required (BSD stat detected?)"
ADMIN_HOME=$(getent passwd "$ADMIN_USER" | cut -d: -f6)
[ -n "$ADMIN_HOME" ] || fail "cannot resolve home directory for $ADMIN_USER"
CAFILE_SRC="${SECURITY_PROXY_CAFILE:-$ADMIN_HOME/.globus/cern-ca-bundle.pem}"
[ -f "$CAFILE_SRC" ] || fail "CA bundle not found: $CAFILE_SRC (set SECURITY_PROXY_CAFILE=...)"
if [ ! -f "$LOCK" ]; then
  fail "missing hash-locked requirements: $LOCK
The macOS requirements.lock is platform/interpreter specific and will NOT verify here.
Generate a Linux lock on this host, e.g.:
    python3 -m venv /tmp/lockenv && /tmp/lockenv/bin/pip install pip-tools
    /tmp/lockenv/bin/pip-compile --generate-hashes --output-file requirements-linux.lock \\
        --extra-index-url https://pypi.org/simple $SRC_DIR/pyproject.toml
Or point at an existing one:  SECURITY_PROXY_LOCK=/path/to/lock sudo -E bash $0"
fi
NOLOGIN=/usr/sbin/nologin
[ -x "$NOLOGIN" ] || NOLOGIN=/sbin/nologin
[ -x "$NOLOGIN" ] || NOLOGIN=/bin/false

echo "==> dedicated user + groups"
for _g in "$PROXY_GROUP" "$CLIENT_GROUP" "$PROVISION_GROUP"; do
  getent group "$_g" >/dev/null || groupadd --system "$_g"
done
if ! getent passwd "$PROXY_USER" >/dev/null; then
  useradd --system --gid "$PROXY_GROUP" --home-dir /nonexistent --no-create-home \
          --shell "$NOLOGIN" --comment "Security Proxy" "$PROXY_USER"
fi
# The daemon creates the sockets; the human fetches tokens and provisions secrets.
usermod -aG "$CLIENT_GROUP,$PROVISION_GROUP" "$PROXY_USER"
usermod -aG "$CLIENT_GROUP,$PROVISION_GROUP" "$ADMIN_USER"

echo "==> directories"
for _path in "$LIB" "$ETC" "$RUN" "$AGENT_RUN" "$INGEST_RUN" "$STATE" \
             "$ETC/config.json" "$UNIT" "$TMPFILES"; do
  _reject_symlink_path "$_path"
  _reject_writable_parent_chain "$_path"
done
install -d -o root -g root -m 0755 "$LIB" "$ETC"
install -d -o root -g root -m 0755 "$RUN"
_install_setgid_dir "$AGENT_RUN"   "$PROXY_USER" "$CLIENT_GROUP"    2750
_install_setgid_dir "$INGEST_RUN"  "$PROXY_USER" "$PROVISION_GROUP" 2750
install -d -o "$PROXY_USER" -g "$PROXY_GROUP" -m 0700 "$STATE"
chmod 0700 "$STATE"
_assert_path "$LIB" dir root root 0755
_assert_path "$ETC" dir root root 0755
_assert_path "$RUN" dir root root 0755
_assert_path "$AGENT_RUN" dir "$PROXY_USER" "$CLIENT_GROUP" 2750
_assert_path "$INGEST_RUN" dir "$PROXY_USER" "$PROVISION_GROUP" 2750
_assert_path "$STATE" dir "$PROXY_USER" "$PROXY_GROUP" 0700

echo "==> runtime dirs recreated at boot (/run is tmpfs)"
TMPFILES_TMP=$(mktemp "$TMPFILES.XXXXXX")
cat > "$TMPFILES_TMP" <<TMPFILES_EOF
# security-proxy runtime sockets. /run is tmpfs, so these must be recreated at boot.
# The setgid bit makes each socket inherit its directory's group, which is what keeps
# token-clients and secret-provisioners separated.
d $RUN 0755 root root -
d $AGENT_RUN 2750 $PROXY_USER $CLIENT_GROUP -
d $INGEST_RUN 2750 $PROXY_USER $PROVISION_GROUP -
TMPFILES_EOF
_require_regular_or_absent "$TMPFILES"
chown root:root "$TMPFILES_TMP"
chmod 0644 "$TMPFILES_TMP"
mv -f "$TMPFILES_TMP" "$TMPFILES"
TMPFILES_TMP=
_assert_path "$TMPFILES" file root root 0644
systemd-tmpfiles --create "$TMPFILES" >/dev/null 2>&1 || true

echo "==> code + venv (deps from hash-checked lock)"
for _path in "$LIB/security_proxy.py" "$LIB/pyproject.toml" "$LIB/requirements.lock" \
             "$ETC/cern-ca-bundle.pem"; do
  _require_regular_or_absent "$_path"
done
install -m 0644 "$SRC_DIR/security_proxy.py" "$LIB/security_proxy.py"
install -m 0644 "$SRC_DIR/pyproject.toml"     "$LIB/pyproject.toml"
install -m 0644 "$LOCK"                       "$LIB/requirements.lock"
python3 -m venv "$LIB/venv"
"$LIB/venv/bin/pip" install -q --disable-pip-version-check --no-cache-dir \
  --require-hashes -r "$LIB/requirements.lock"
"$LIB/venv/bin/pip" install -q --disable-pip-version-check --no-cache-dir \
  --no-deps --no-build-isolation -e "$LIB"
chown -R -h root:root "$LIB"
find "$LIB" ! -type l -exec chmod go-w {} +
_assert_path "$LIB/security_proxy.py" file root root 0644
_assert_path "$LIB/pyproject.toml" file root root 0644
_assert_path "$LIB/requirements.lock" file root root 0644

echo "==> CA bundle (non-secret) copied so the daemon user can read it"
install -m 0644 -o root -g root "$CAFILE_SRC" "$ETC/cern-ca-bundle.pem"
_assert_path "$ETC/cern-ca-bundle.pem" file root root 0644

echo "==> config (root-owned; cert+secrets are pushed at runtime, not stored here)"
CONFIG_TMP=$(mktemp "$ETC/config.json.XXXXXX")
cat > "$CONFIG_TMP" <<JSON
{
  "cafile": "$ETC/cern-ca-bundle.pem",
  "cert": {"ingest": "grid-cert"},
  "agent_socket": "$AGENT_RUN/agent.sock",
  "ingest_socket": "$INGEST_RUN/ingest.sock",
  "agent_socket_group": "$CLIENT_GROUP",
  "ingest_socket_group": "$PROVISION_GROUP",
  "secret_rotation_seconds": 86400,
  "routes": [
    {"prefix": "/ccdb/", "upstream": "https://alice-ccdb.cern.ch"},
    {"prefix": "/hyperloop/", "upstream": "https://alimonitor.cern.ch/hyperloop"},
    {"prefix": "/remote-mcp/", "upstream": "wss://alien.cern.ch", "websocket": true},
    {"prefix": "/alimonitor/", "upstream": "https://alimonitor.cern.ch"},
    {"prefix": "/alihyperloop-data/", "upstream": "https://alimonitor.cern.ch/alihyperloop-data"},
    {"prefix": "/bookkeeping/", "upstream": "https://ali-bookkeeping.cern.ch"},
    {"name": "nomad", "upstream": "https://alinomad.cern.ch", "websocket": true,
     "auth_header": "X-Nomad-Token", "inject_headers": {"X-Nomad-Token": {"ingest": "nomad"}}},
    {"name": "consul", "upstream": "https://aliconsul.cern.ch",
     "auth_header": "X-Consul-Token", "inject_headers": {"X-Consul-Token": {"ingest": "consul"}}},
    {"name": "vault", "upstream": "https://alivault.cern.ch",
     "auth_header": "X-Vault-Token", "inject_headers": {"X-Vault-Token": {"ingest": "vault"}}},
    {"name": "s3", "upstream": "https://s3.cern.ch",
     "s3": {"access_key": {"ingest": "s3-access"}, "secret_key": {"ingest": "s3-secret"}}}
  ]
}
JSON
_require_regular_or_absent "$ETC/config.json"
chown root:root "$CONFIG_TMP"
chmod 0644 "$CONFIG_TMP"
mv -f "$CONFIG_TMP" "$ETC/config.json"
CONFIG_TMP=
_assert_path "$ETC/config.json" file root root 0644

echo "==> systemd unit"
UNIT_TMP=$(mktemp "$UNIT.XXXXXX")
cat > "$UNIT_TMP" <<UNIT_EOF
[Unit]
Description=Security Proxy (localhost credential broker for ALICE services)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$PROXY_USER
Group=$PROXY_GROUP
SupplementaryGroups=$CLIENT_GROUP $PROVISION_GROUP
ExecStart=$LIB/venv/bin/python $LIB/security_proxy.py --config $ETC/config.json
WorkingDirectory=$LIB
Restart=always
RestartSec=2
UMask=0077

# Sandboxing: the daemon needs loopback + outbound TLS, its runtime sockets, and the
# oauth refresh_store. Everything else is read-only or hidden.
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
ReadWritePaths=$RUN $STATE

[Install]
WantedBy=multi-user.target
UNIT_EOF
_require_regular_or_absent "$UNIT"
chown root:root "$UNIT_TMP"
chmod 0644 "$UNIT_TMP"
mv -f "$UNIT_TMP" "$UNIT"
UNIT_TMP=
_assert_path "$UNIT" file root root 0644

echo "==> (re)load the daemon"
systemctl daemon-reload
systemctl enable security-proxy >/dev/null 2>&1 || true
systemctl restart security-proxy

cat <<EOF

Done. The daemon is running but UNPROVISIONED (mTLS routes 503 until you push the
cert + tokens).

NOTE: there is no macOS Keychain here, and bootstrap sources are declarative (a
source names what to read, never a command to run), so every slot in
~/.security-proxy-bootstrap.json must use "file" or "vault", e.g.
  {"ingest_socket": "$INGEST_RUN/ingest.sock",
   "agent_socket": "$AGENT_RUN/agent.sock",
   "slots": {
     "vault":     {"file": "~/.vault-token"},
     "grid-cert": {"file": ["~/.globus/usercert.pem", "~/.globus/userkey.pem"]},
     "nomad":     {"vault": {"path": "kv/data/ci", "field": "nomad_token"}},
     "s3-access": {"vault": {"path": "kv/data/ci", "field": "s3_access_key"}},
     "s3-secret": {"vault": {"path": "kv/data/ci", "field": "s3_secret_key"}}}}
A "vault" source is read through the proxy's own vault route, so the "vault" slot
itself has to come from a file (or be pushed by hand) and agent_socket must be set.

Next, as $ADMIN_USER (NOT root):
  1. Start a NEW login session so your new '$CLIENT_GROUP' and
     '$PROVISION_GROUP' group memberships apply (log out/in, or \`newgrp\`).
  2. security-proxy-bootstrap --socket $INGEST_RUN/ingest.sock
  3. eval "\$(printf 'export NOMAD_ADDR=%s NOMAD_TOKEN=%s' \\
       "\$(security-proxy-token --socket $AGENT_RUN/agent.sock --addr)" \\
       "\$(security-proxy-token --socket $AGENT_RUN/agent.sock nomad)")"
     nomad node status

Logs:    journalctl -u security-proxy -f
Restart: sudo systemctl restart security-proxy   # then re-run bootstrap
EOF
