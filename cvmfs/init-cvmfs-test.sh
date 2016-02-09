#!/bin/bash -e
SRC=/cvmfs/alice.cern.ch
DST=alicvmfs-test03:/cvmfs/alice-test.cern.ch
DIRS=( bin etc )
DIRS+=( $(cd $SRC && find . -maxdepth 4 -type d -name BASE) )
DIRS+=( $(cd $SRC && find . -maxdepth 3 -type d -name versions) )
DIRS+=( $(cd $SRC && find . -maxdepth 3 -regextype egrep -regex '.*/([0-9\.])+') )
for D in "${DIRS[@]}"; do
  DST_FULL=$DST/$D
  DST_FULL=${DST_FULL#*:}
  echo $DST_FULL
  rsync --rsync-path="mkdir -p $DST_FULL && rsync" -a --delete $SRC/$D/ $DST/$D/
done
echo all synced
