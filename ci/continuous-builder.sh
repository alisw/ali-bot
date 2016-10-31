#!/bin/bash
# A simple script which keeps building using the latest aliBuild,
# alidist and AliRoot / AliPhysics.
# Notice this will do an incremental build, not a full build, so it
# really to catch errors earlier.

while true; do
  if [ ! -e alibuild ]; then
    git clone https://github.com/alisw/alibuild
  fi
  if [ ! -e alidist ]; then
    git clone https://github.com/alisw/alidist
  fi
  if [ ! -e AliPhysics ]; then
    git clone http://git.cern.ch/pub/AliPhysics
  fi
  if [ ! -e AliPhysics ]; then
    git clone http://git.cern.ch/pub/AliRoot
  fi
  for d in alibuild alidist AliRoot AliPhysics; do
    pushd $d
      git pull origin
    popd
  done

  aliBuild/aliBuild build AliPhysics
done
