#!/bin/bash -ex
set -o pipefail
TMPDIR=$(mktemp -d /tmp/aliPublish.XXXXX)
TORDIR=/var/packages/download
TORNOTIFY=/var/packages/NEWFILE
mkdir -p "$TMPDIR"
cd "$TMPDIR"
curl --silent -L "%(url)s" | tar --strip-components=3 -xzf -
TAR="$(echo "%(package)s"|tr '[:upper:]' '[:lower:]')_%(version)s.%(arch)s.tar.gz"
[[ -e "%(version)s/etc/modulefiles/%(package)s" ]]
tar czf "$TAR" "%(version)s/"
rm -rf "%(version)s/"
DEPS="%(dependencies)s"
SE=$(curl -L 'http://alimonitor.cern.ch/services/getBestSE.jsp?count=1&op=0' | \
     head -n1 | grep -E '^[^: ]+::[^: ]+::[^: ]+$')
[[ "$SE" != '' ]] || SE="ALICE::CERN::EOS"
echo "Using SE $SE"
alien -exec packman define "%(package)s" "%(version)s" \
            "$TMPDIR/$TAR" \
            ${DEPS:+dependencies=$DEPS} \
            -vo -platform %(arch)s -se $SE
cp -f "$TMPDIR/$TAR" "$TORDIR/$TAR"
chmod a=rw "$TORDIR/$TAR"
touch "$TORNOTIFY"
alien -exec addMirror \
            "/alice/packages/%(package)s/%(version)s/%(arch)s" \
            no_se "torrent://alitorrent.cern.ch/torrents/$TAR.torrent"
rm -rf "$TMPDIR"
