#!/bin/bash -ex
set -o pipefail
TMPDIR=$(mktemp -d /tmp/aliPublish.XXXXX)
TORDIR=/var/packages/download
TORNOTIFY=/var/packages/NEWFILE
mkdir -p "$TMPDIR"
cd "$TMPDIR"
curl --silent -L "%(url)s" | tar --strip-components=3 -xzf -
TAR="$(echo "%(package)s"|tr '[:upper:]' '[:lower:]')_%(version)s.%(arch)s.tar.gz"
tar czf "$TAR" "%(version)s/"
rm -rf "%(version)s/"
DEPS="%(dependencies)s"
alien -exec packman define "%(package)s" "%(version)s" \
            "$TMPDIR/$TAR" \
            ${DEPS:+dependencies=$DEPS} \
            -vo -platform %(arch)s -se ALICE::CERN::EOS
cp -f "$TMPDIR/$TAR" "$TORDIR/$TAR"
chmod a=rw "$TORDIR/$TAR"
touch "$TORNOTIFY"
alien -exec addMirror \
            "/alice/packages/%(package)s/%(version)s/%(arch)s" \
            no_se "torrent://alitorrent.cern.ch/torrents/$TAR.torrent"
rm -rf "$TMPDIR"
