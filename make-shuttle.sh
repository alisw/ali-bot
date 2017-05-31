#!/bin/bash -e
type aliBuild &> /dev/null
[[ $# == 1 ]] || { printf "Please specify an AliRoot tag as the only argument.\n"; exit 1; }
mkdir -p ~/alibuild/sw/MIRROR && cd ~/alibuild
[[ -d alidist/.git ]] || git clone https://github.com/alisw/alidist alidist/
[[ -d sw/MIRROR/aliroot ]] || git clone --bare https://github.com/alisw/AliRoot sw/MIRROR/aliroot
[[ -d AliRoot ]] || git clone --reference sw/MIRROR/aliroot https://github.com/alisw/AliRoot AliRoot/
[[ -d AliRoot-OCDB ]] || git clone --depth 1 https://gitlab.cern.ch/alisw/AliRootOCDB.git AliRoot-OCDB/
if [[ $1 != master && ! $MAKE_SHUTTLE_RETRY ]]; then
  [[ -d AliRoot_master ]] || git clone --reference sw/MIRROR/aliroot https://github.com/alisw/AliRoot AliRoot_master/
  pushd AliRoot_master
    # Upstream master of AliRoot
    git remote update -p && git fetch && git fetch --tags
    git clean -fxd
    git checkout master
    git reset --hard origin/master
  popd
fi
pushd AliRoot
  # Upstream user-defined tag of AliRoot
  git remote update -p && git fetch && git fetch --tags
  git clean -fxd
  git checkout master
  git reset --hard refs/tags/$1 || git reset --hard origin/$1 || git reset --hard $1
popd
[[ $1 != master && ! $MAKE_SHUTTLE_RETRY ]] && rsync -av --delete AliRoot_master/SHUTTLE/ AliRoot/SHUTTLE/ || true
( cd AliRoot && git status )
# If we are building the master and the build fails, fall back to the latest
# working version. Do not fall back in case we are building a tag.
aliBuild build AliRoot --debug --defaults shuttle || \
  { [[ $1 != master || $MAKE_SHUTTLE_RETRY ]] && exit 1; } || \
  { export MAKE_SHUTTLE_RETRY=1; exec "$0" $(cat latest_working_hash); }
LOCAL_HASH=$( cd AliRoot && git rev-parse HEAD )
UPSTREAM_HASH=$( cd AliRoot && git rev-parse origin/master )
SHUTTLE_HASH=$( cd AliRoot$([[ $1 != master && ! $MAKE_SHUTTLE_RETRY ]] && echo _master || true) && git log -1 --format=%H SHUTTLE )
printf $LOCAL_HASH > latest_working_hash
printf "\n\033[1;32mAliRoot version $LOCAL_HASH ($1) compiled OK\n"
printf "Last SHUTTLE commit taken is $SHUTTLE_HASH from upstream AliRoot $UPSTREAM_HASH\033[m\n\n"
