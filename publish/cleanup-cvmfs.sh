#!/bin/bash -e
LIST=toClean.txt
MPREFIX=/cvmfs/alice.cern.ch/x86_64-2.6-gnu-4.1.2/Modules
PPREFIX=$(cd $MPREFIX/../Packages;pwd)
ENABLED_MPREFIX=$MPREFIX/modulefiles
ARCHIVED_MPREFIX=$MPREFIX/archive
REMOVE=1
[[ -e $LIST ]]
while read P; do
  PKGNAME=${P%% *}
  PKGVER=${P#* }
  [[ "$PKGNAME" != "$P" ]]
  [[ "$PKGVER" != "$P" ]]
  echo -n "$([[ "$REMOVE" == 1 ]] && echo Removing || echo Archiving) $PKGNAME $PKGVER: "
  if [[ ! -f $ENABLED_MPREFIX/$PKGNAME/$PKGVER ]]; then
    [[ -f $ARCHIVED_MPREFIX/$PKGNAME/$PKGVER ]] \
      && echo skipping, already archived \
      || echo WARNING, cannot find either enabled or archived copy
    continue
  fi
  if [[ "$REMOVE" == 1 ]]; then
    rm -rf $PPREFIX/$PKGNAME/$PKGVER \
           $ENABLED_MPREFIX/$PKGNAME/$PKGVER
  else
    mkdir -p $ARCHIVED_MPREFIX/$PKGNAME
    mv $ENABLED_MPREFIX/$PKGNAME/$PKGVER $ARCHIVED_MPREFIX/$PKGNAME/$PKGVER
  fi
  echo done
done < <(cat $LIST)
echo All condemned packages processed
