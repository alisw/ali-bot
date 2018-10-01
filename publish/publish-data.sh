#!/bin/bash -ex

# Use FORCE=1 to force this script to tag data dir even if we are before 4pm.
# Use DRYRUN=1 to dump tree to /tmp/cvmfs/... instead of the real CVMFS server.
#
# You can do that by running:
#   env DRYRUN=1 FORCE=1 ./publish-data.sh

dieabort() {
  cd /
  rm -rf $DEST
  cvmfs_server abort -f || true
  exit 1
}

cvmfs_lazy_transaction() {
  [[ $CVMFS_IN_TRANSACTION ]] && return 0
  for I in {0..7}; do
    ERR=0
    cvmfs_server transaction && CVMFS_IN_TRANSACTION=1 || ERR=$?
    [[ $ERR == 0 ]] && break || sleep 7
  done
  return $ERR
}

cvmfs_lazy_publish() {
  [[ $CVMFS_IN_TRANSACTION ]] && { cvmfs_server publish || return $?; }
  return 0
}

CVMFS_IN_TRANSACTION=
export PATH=$HOME/opt/bin:$PATH
[[ $DRYRUN ]] || { cvmfs_server &> /dev/null || [[ $? != 127 ]]; }
sshpass &> /dev/null || [[ $? != 127 ]]
[[ -e $HOME/.eossynccreds ]]
REPO=$(cvmfs_server info | grep 'Repository name' | cut -d: -f2 | xargs echo)
[[ $REPO == *.cern.ch ]] || REPO=alice.cern.ch
[[ $DRYRUN ]] && cvmfs_server() { echo "[fake] cvmfs_server $*"; } || true
SRC=lxplus.cern.ch:/eos/experiment/alice/analysis-data
RO_DEST_PREFIX=/cvmfs/$REPO/data/analysis
[[ $DRYRUN ]] && DEST_PREFIX=/tmp$RO_DEST_PREFIX || DEST_PREFIX=$RO_DEST_PREFIX

if [[ ! -d $DEST_PREFIX ]]; then
  cvmfs_lazy_transaction || dieabort
  mkdir -p $DEST_PREFIX
fi

for ARCH_DIR in /cvmfs/$REPO/{el*,ubuntu*}; do
  ARCH_DIR=$ARCH_DIR/Modules/modulefiles/AliPhysics
  [[ -d $ARCH_DIR ]] || continue
  pushd $ARCH_DIR &> /dev/null
    for ALIPHYSICS in *; do
      [[ -e $ALIPHYSICS ]] || continue  # deal with * not expanded
      [[ $ALIPHYSICS != vAN-* ]] || continue  # exclude AN tags
      [[ $ALIPHYSICS != v5-06* && $ALIPHYSICS != v5-07* && $ALIPHYSICS != v5-08* ]] || continue  # from v5-09 on

      SNAPSHOT_DEST=$(cd $DEST_PREFIX/..;pwd)/prod/${ALIPHYSICS%-*}
      [[ -e $SNAPSHOT_DEST ]] && continue  # already exists: skipping

      TAG_CREATION_TIMESTAMP=$(stat -c%Y $ALIPHYSICS)

      # We don't fetch data from EOS. Instead, we re-snapshot the most recent
      # daily snapshot. We start from the tag creation date, and we go back up
      # to two days (three total attempts). No suitable snapshot --> failure.
      # We only symlink instead of copying data (even if CVMFS deduplicates).
      echo Need to create snapshot for tag: $SNAPSHOT_DEST
      for ((I=0; I<3; I++)); do
        SNAPSHOT_DATE=$(date -u -d @$((TAG_CREATION_TIMESTAMP-86400*I)) +%Y/vAN-%Y%m%d)
        SNAPSHOT_SOURCE=$RO_DEST_PREFIX/$SNAPSHOT_DATE
        [[ -d $SNAPSHOT_SOURCE ]] || { echo does not exist, $SNAPSHOT_SOURCE; continue; }
        cvmfs_lazy_transaction || dieabort
        mkdir -p $(dirname $SNAPSHOT_DEST)
        ln -nfs ../analysis/$SNAPSHOT_DATE $SNAPSHOT_DEST
        break
      done
      [[ -L $SNAPSHOT_DEST && -d $SNAPSHOT_SOURCE ]] || dieabort
      echo Created from $SNAPSHOT_SOURCE

    done
  popd &> /dev/null
done

# Take care of today's snapshot from EOS
DEST=$DEST_PREFIX/$(TZ=Europe/Rome date +%Y/vAN-%Y%m%d)
[[ -d $DEST && ! $FORCE ]] && { echo "Published already: $DEST"; cvmfs_lazy_publish; exit $?; } || true
[[ $(TZ=Europe/Rome date +%_H%M) -lt 1600 && ! $FORCE ]] && { echo "Before 4pm, doing nothing"; cvmfs_lazy_publish; exit 0; } || true
cvmfs_lazy_transaction || dieabort
mkdir -p $DEST && [[ -d $DEST ]] || dieabort
SRC_YESTERDAY_SHORT=$( date -d @$(($(TZ=Europe/Rome date +%s) - 86400)) +%Y/vAN-%Y%m%d )
SRC_YESTERDAY=$DEST_PREFIX/$SRC_YESTERDAY_SHORT
if [[ -d $SRC_YESTERDAY ]]; then
  # CVMFS's local disk is much faster than EOS, and OADB does not change frequently: we first rsync
  # yesterday's dir into today's, and then we rsync (--delete) from EOS to today's dir
  rsync -av --delete $SRC_YESTERDAY/ $DEST/ || dieabort
fi
rsync -av --delete --rsh="sshpass -p `cat $HOME/.eossynccreds|cut -d: -f2-` ssh -oUserKnownHostsFile=/dev/null -oStrictHostKeyChecking=no -l `cat $HOME/.eossynccreds|cut -d: -f1`" $SRC/ $DEST/ || dieabort
chmod -R u=rwX,g=rX,o=rX $DEST/
touch $DEST
while read FILE; do
  mv -v $FILE ${FILE%*.no_access}
done < <(find $DEST -name *.no_access)
if diff -x .cvmfscatalog --brief -r $SRC_YESTERDAY/ $DEST/ &> /dev/null; then
  # Nothing changed from yesterday: replace directory with a symlink.
  # If yesterday's source is already a symlink, resolve it first
  LINKDEST=$(readlink $SRC_YESTERDAY 2> /dev/null || true)
  [[ $LINKDEST ]] || LINKDEST=../$SRC_YESTERDAY_SHORT
  rm -rf $DEST  # no final slash!
  ln -nfs $LINKDEST $DEST
fi
cvmfs_lazy_publish || dieabort
echo "All OK"
