#!/bin/bash -ex
set -o pipefail
cd "/cvmfs/%(repo)s"
mkdir -p "%(arch)s/Packages"
cd "%(arch)s/Packages"
curl --silent -L "%(url)s" | tar --strip-components=2 -xzf -
# Dereference hardlinks: CVMFS does not support them across dirs
find "%(pkgdir)s" -not -type d -links +1 -exec \
  sh -ec 'cp -ip "{}" "{}".__DEREF__; mv "{}".__DEREF__ "{}"' \;
export WORK_DIR="$PWD"
export PKGPATH="%(package)s/%(version)s"
sh -e "%(pkgdir)s/relocate-me.sh"
MODULESRC="%(pkgdir)s/etc/modulefiles/%(package)s"
MODULEDST="%(modulefile)s"
[[ -e "$MODULESRC" ]]
mkdir -p "$(dirname "$MODULEDST")"
cp -v "$MODULESRC" "$MODULEDST"
