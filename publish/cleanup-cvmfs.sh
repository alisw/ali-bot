#!/bin/bash -e
LIST=toClean.txt
PREFIX=/cvmfs/alice.cern.ch/x86_64-2.6-gnu-4.1.2/Modules
ENABLED_PREFIX=$PREFIX/modulefiles
ARCHIVED_PREFIX=$PREFIX/archive
[[ -e $LIST ]]
while read P; do
  PKGNAME=${P%% *}
  PKGVER=${P#* }
  [[ "$PKGNAME" != "$P" ]]
  [[ "$PKGVER" != "$P" ]]
  echo -n "Doing $PKGNAME $PKGVER: "
  if [[ ! -f $ENABLED_PREFIX/$PKGNAME/$PKGVER ]]; then
    [[ -f $ARCHIVED_PREFIX/$PKGNAME/$PKGVER ]] \
      && echo skipping, already archived \
      || echo WARNING, cannot find either enabled or archived copy
    continue
  fi
  mkdir -p $ARCHIVED_PREFIX/$PKGNAME
  mv $ENABLED_PREFIX/$PKGNAME/$PKGVER $ARCHIVED_PREFIX/$PKGNAME/$PKGVER
  echo done
done < <(cat $LIST)
echo All condemned packages archived
