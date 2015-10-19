#!/bin/bash -ex
set -o pipefail
if [[ -x /home/monalisa/bin/alien ]]; then
  export PATH="/home/monalisa/bin:$PATH"
  CMD="--no-notification sync-alien"
elif [[ -d /cvmfs ]]; then
  CMD="sync-cvmfs"
else
  false
fi
CMD="$CMD"
DEST=ali-bot
DRYRUN=${DRYRUN:-}
export LANG=C
cd "$(dirname "$0")"
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
  git reset --hard origin/master
popd
ln -nfs $(basename $LOG.error) log/latest
( cd $DEST/publish
  echo "Running version $(git rev-parse HEAD)"
  ./aliPublish --debug ${DRYRUN:+--dry-run} ${CMD} ) 2>&1 | tee -a $LOG.error
mv -v $LOG.error $LOG
ln -nfs $(basename $LOG) log/latest
echo "All went right, self-updating now"
[[ -x $DEST/publish/get-and-run.sh ]] && exec cp -v $DEST/publish/get-and-run.sh .
