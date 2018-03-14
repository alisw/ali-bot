#!/bin/bash -ex
set -o pipefail
SSLVERIFY=%(http_ssl_verify)d
CONNTIMEOUT=%(conn_timeout_s)d
CONNRETRY=%(conn_retries)d
CONNRETRYDELAY=%(conn_dethrottle_s)d
[[ $SSLVERIFY == 0 ]] && SSLVERIFY=-k || SSLVERIFY=
TMPDIR=$(mktemp -d /tmp/aliPublish.XXXXX)
mkdir -p "$TMPDIR"
cd "$TMPDIR"

# Create dummy tarball to make Packman happy
TAR="$(echo "%(package)s"|tr '[:upper:]' '[:lower:]')_%(version)s.%(arch)s.tar.gz"
mkdir "%(version)s"
echo "$(package)s %(version)s %(arch)s" > "%(version)s/package.txt" 
tar czf "$TAR" "%(version)s/"
rm -rf "%(version)s/"

BEST_SES=$(curl -sL 'http://alimonitor.cern.ch/services/getBestSE.jsp?count=4&op=0' | \
           grep -E '^[^: ]+::[^: ]+::[^: ]+$' | sort -R)
[[ "$BEST_SES" != '' ]] || BEST_SES="ALICE::CERN::EOS"
echo "Best storage elements found: $BEST_SES"
ERR=1
for SE in $BEST_SES; do
  echo "Trying SE $SE"
  alien -exec packman define "%(package)s" "%(version)s" \
              "$TMPDIR/$TAR"                             \
              -vo -platform %(arch)s -se $SE || continue
  ERR=0
done
[[ $ERR == 1 ]] && { echo "All storage elements failed"; exit 1; } || true
cd /
rm -rf "$TMPDIR"
