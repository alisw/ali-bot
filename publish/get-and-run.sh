#!/bin/bash -ex
set -o pipefail
export LANG=C
cd "$(dirname "$0")"
if [[ -x /home/monalisa/bin/alien ]]; then
  export PATH="/home/monalisa/bin:$PATH"
  CMD=sync-alien
elif [[ -d /lustre/atlas/proj-shared/csc108 && -d /lustre/atlas/proj-shared/csc108 ]]; then
  # Titan needs some magic.
  source /usr/share/Modules/init/bash
  eval $(modulecmd bash load git/2.2.0)
  git --help > /dev/null 2>&1
  FAKECVMFS=/lustre/atlas/proj-shared/csc108/psvirin/publisher/.fakecvmfs
  mkdir -p $FAKECVMFS
  ln -nfs $(which true) $FAKECVMFS/cvmfs_server
  export PATH="$FAKECVMFS:$PATH"
  [[ ! -e alibuild/.git ]] && git clone https://github.com/alisw/alibuild
  [[ ! -e requests/.git ]] && git clone https://github.com/kennethreitz/requests -b v2.6.0
  export PYTHONPATH="$PWD/alibuild:$PWD/requests:$PYTHONPATH"
  CONF=aliPublish-titan.conf
  CMD=sync-cvmfs
elif [[ -d /cvmfs/alice-test.cern.ch ]]; then
  CONF=aliPublish-test.conf
  CMD=sync-cvmfs
elif [[ -d /cvmfs/alice-nightlies.cern.ch ]]; then
  CONF=aliPublish-nightlies.conf
  CMD=sync-cvmfs
elif [[ -d /cvmfs ]]; then
  CMD=sync-cvmfs
else
  false
fi
CMD="$CMD"
DEST=ali-bot
DRYRUN=${DRYRUN:-}
[[ ! -e $DEST/.git ]] && git clone https://github.com/alisw/ali-bot $DEST
mkdir -p log
find log/ -type f -mtime +3 -delete || true
LOG="log/log-$(date -u +%Y%m%d-%H%M%S%z)"
pushd $DEST
  git clean -fd
  git clean -fxd
  git remote update -p
  git fetch
  git fetch --tags
  git reset --hard origin/$(git rev-parse --abbrev-ref HEAD)
popd
ln -nfs $(basename $LOG.error) log/latest
CACHE=$PWD/cache
mkdir -p $CACHE
( cd $DEST/publish
  echo "Running version $(git rev-parse HEAD)"
  ./aliPublish --debug                        \
               ${DRYRUN:+--dry-run}           \
               ${NO_NOTIF:+--no-notification} \
               ${CONF:+--config "$CONF"}      \
               --cache-deps-dir $CACHE        \
               --pidfile /tmp/aliPublish.pid  \
               $CMD ) 2>&1 | tee -a $LOG.error
mv -v $LOG.error $LOG
ln -nfs $(basename $LOG) log/latest
echo "All went right, self-updating now"
[[ -x $DEST/publish/get-and-run.sh ]] && exec cp -v $DEST/publish/get-and-run.sh .
