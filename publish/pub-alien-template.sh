#!/bin/bash -ex
set -o pipefail
SSLVERIFY=%(http_ssl_verify)d
CONNTIMEOUT=%(conn_timeout_s)d
CONNRETRY=%(conn_retries)d
CONNRETRYDELAY=%(conn_dethrottle_s)d
[[ $SSLVERIFY == 0 ]] && SSLVERIFY=-k || SSLVERIFY=
TMPDIR=$(mktemp -d /tmp/aliPublish.XXXXX)
mkdir -p "$TMPDIR"
trap -- 'rm -rf "$TMPDIR"' EXIT TERM
cd "$TMPDIR"
TAR="$(echo "%(package)s"|tr '[:upper:]' '[:lower:]')_%(version)s.%(arch)s.tar.gz"
curl -Lsf $SSLVERIFY                \
     --connect-timeout $CONNTIMEOUT \
     --retry-delay $CONNRETRYDELAY  \
     --retry $CONNRETRY "%(url)s" -o "$TAR"

DEPS="%(dependencies)s"
BEST_SES=$(curl -sL 'http://alimonitor.cern.ch/services/getBestSE.jsp?count=4&op=0' | \
           grep -E '^[^: ]+::[^: ]+::[^: ]+$')
[[ "$BEST_SES" != '' ]] || BEST_SES="ALICE::CERN::EOS"
echo "Best storage elements found: $BEST_SES"
ERR=1
for SE in $BEST_SES; do
  echo "Trying SE $SE"
  alien -exec packman define "%(package)s" "%(version)s" \
              "$TMPDIR/$TAR"                             \
              ${DEPS:+dependencies=$DEPS}                \
              -vo -platform %(arch)s -se $SE || continue
  ERR=0
done
[[ $ERR == 1 ]] && { echo "All storage elements failed"; exit 1; } || true
