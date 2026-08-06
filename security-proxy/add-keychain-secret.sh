#!/bin/bash
# Store a secret in the macOS Keychain without it ever reaching argv or shell history.
#
# `security add-generic-password -w <value>` puts the secret on the command line, where
# anyone on the box can read it out of `ps`. Bare `-w` avoids that but prompts twice
# (value + retype), so it does not compose with a pipe. `security -i` takes the whole
# subcommand -- secret included -- on stdin, leaving the process argv as just
# `security -i`. That is the trick keychain_set_token() uses in security_proxy.py, and
# this is its shell equivalent.
#
# Usage:
#   add-keychain-secret.sh -s <service> [-a <account>] [-k <keychain>] [-p <prefix>]
#
#   -s  service name (the key you look it up by)          [required]
#   -a  account                                           [default: $USER]
#   -k  keychain file; omit to use the default search list
#   -p  string prepended to what you type, e.g. 'Bearer ' for a header-value slot
#   -A  leave the default trusted-application list alone (default: trust nothing, so
#       every read prompts)
#
# The value is read from the terminal with echo off, or from stdin when piped:
#   ./add-keychain-secret.sh -s github-token-rw -p 'Bearer ' \
#       -k ~/Library/Keychains/security-proxy.keychain-db

set -euo pipefail

die() { printf '%s: %s\n' "${0##*/}" "$1" >&2; exit 1; }

service=; account=$USER; keychain=; prefix=; trust_none=1
while getopts ':s:a:k:p:Ah' opt; do
  case $opt in
    s) service=$OPTARG ;;
    a) account=$OPTARG ;;
    k) keychain=$OPTARG ;;
    p) prefix=$OPTARG ;;
    A) trust_none=0 ;;
    h) awk 'NR>1 && /^#/ {print; next} NR>1 {exit}' "$0"; exit 0 ;;
    :) die "-$OPTARG needs an argument" ;;
    *) die "unknown option -$OPTARG (try -h)" ;;
  esac
done
[ -n "$service" ] || die "missing -s <service> (try -h)"

if [ -t 0 ]; then
  read -rsp "value for '$service': " value; echo
else
  IFS= read -r value || true      # piped: take the first line, ignore a trailing newline
fi
[ -n "$value" ] || die "empty value, nothing stored"

value=$prefix$value

# `security -i` reads line by line, so a newline in any field would end this command and
# start another one -- i.e. command injection. Single quotes would break out of the
# quoting below; no credential we store here contains either.
case $value$service$account$keychain in
  *$'\n'*|*$'\r'*) die "value/arguments must not contain newlines" ;;
  *\'*)            die "value/arguments must not contain single quotes" ;;
esac

# Everything is single-quoted, so the secret is a literal to security's parser.
cmd="add-generic-password -U -s '$service' -a '$account' -w '$value'"
[ "$trust_none" -eq 1 ] && cmd="$cmd -T ''"
[ -n "$keychain" ] && cmd="$cmd '$keychain'"

printf '%s\n' "$cmd" | security -i || die "keychain write failed"
unset value cmd

printf "stored '%s' for account '%s'%s\n" \
  "$service" "$account" "${keychain:+ in $keychain}"
