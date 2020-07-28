#!/bin/bash -ex
set -o pipefail
PACKAGES_DIR="$(dirname "$(dirname "%(pkgdir)s")")"
mkdir -p "$PACKAGES_DIR"
cd "$PACKAGES_DIR"
SSLVERIFY=%(http_ssl_verify)d
CONNTIMEOUT=%(conn_timeout_s)d
CONNRETRY=%(conn_retries)d
CONNRETRYDELAY=%(conn_dethrottle_s)d
[[ $SSLVERIFY == 0 ]] && SSLVERIFY=-k || SSLVERIFY=
curl -Lsf $SSLVERIFY                \
     --connect-timeout $CONNTIMEOUT \
     --retry-delay $CONNRETRYDELAY  \
     --retry $CONNRETRY "%(url)s"   | tar --strip-components=2 -xzf -
# Dereference hardlinks: CVMFS does not support them across dirs
find "%(pkgdir)s" -not -type d -links +1 -exec \
  sh -ec 'cp -ip "{}" "{}".__DEREF__; mv "{}".__DEREF__ "{}"' \;
export WORK_DIR="$PWD"
export PKGPATH="%(package)s/%(version)s"
sh -e "%(pkgdir)s/relocate-me.sh"
MODULESRC="%(pkgdir)s/etc/modulefiles/%(package)s"
MODULEDST="%(modulefile)s"
if [ -e "$MODULESRC" ]; then
  mkdir -p "$(dirname "$MODULEDST")"
  cp -v "$MODULESRC" "$MODULEDST"
fi
[[ ! "%(repo)s" ]] || exit 0
# Only in sync-dir mode: create BASE/1.0
BASEDST=$(dirname $(dirname $MODULEDST))/BASE/1.0
mkdir -p $(dirname $BASEDST)
echo -e "#%%Module\nsetenv BASEDIR $PACKAGES_DIR" > $BASEDST
