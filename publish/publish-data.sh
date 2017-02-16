#!/bin/bash -ex
cvmfs_server &> /dev/null || [[ $? != 127 ]]
sshpass &> /dev/null || [[ $? != 127 ]]
[[ -e $HOME/.eossynccreds ]]
REPO=$(cvmfs_server info | grep 'Repository name' | cut -d: -f2 | xargs echo)
[[ $REPO == *.cern.ch ]] || REPO=alice.cern.ch
[[ $DRYRUN ]] && cvmfs_server() { echo "[fake] cvmfs_server $*"; } || true
SRC=lxplus.cern.ch:/eos/experiment/alice/analysis-data
DEST=/cvmfs/$REPO/data/analysis/$(TZ=Europe/Rome date +%Y/vAN-%Y%m%d)
[[ $DRYRUN ]] && DEST=/tmp$DEST || true
[[ -d $DEST && ! $FORCE ]] && { echo "Published already: $DEST"; exit 0; } || true
[[ $(TZ=Europe/Rome date +%_H%M) -lt 1600 && ! $FORCE ]] && { echo "Before 4pm, doing nothing"; exit 0; } || true
for I in {0..7}; do
  ERR=0
  cvmfs_server transaction || ERR=$?
  [[ $ERR == 0 ]] && break || sleep 7
done
[[ $ERR == 0 ]]
dieabort() {
  rm -rf $DEST
  cvmfs_server abort -f || true
  exit 1
}
mkdir -p $DEST && [[ -d $DEST ]] || dieabort
rsync -av --delete --rsh="sshpass -p `cat $HOME/.eossynccreds|cut -d: -f2-` ssh -oUserKnownHostsFile=/dev/null -oStrictHostKeyChecking=no -l `cat $HOME/.eossynccreds|cut -d: -f1`" $SRC/ $DEST/ || dieabort
chmod -R u=rwX,g=rX,o=rX $DEST/
touch $DEST
cvmfs_server publish || dieabort
echo "All OK"
