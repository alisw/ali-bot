#!/bin/bash
# A simple script which keeps building using the latest aliBuild,
# alidist and AliRoot / AliPhysics.
# Notice this will do an incremental build, not a full build, so it
# really to catch errors earlier.

MIRROR=${MIRROR:-/build/mirror}
ALIBUILD_REPO=${ALIBUILD_REPO:-alisw/alibuild}
ALIDIST_REPO=${ALIDIST_REPO:-alisw/alidist}

while true; do
  if [ ! -e alibuild ]; then
    git clone https://github.com/$ALIBUILD_REPO
  fi
  if [ ! -e alidist ]; then
    git clone https://github.com/$ALIDIST_REPO
  fi
  if [ ! -e AliPhysics ]; then
    git clone http://git.cern.ch/pub/AliPhysics
  fi
  if [ ! -e AliRoot ]; then
    git clone http://git.cern.ch/pub/AliRoot
  fi
  for d in alibuild alidist AliRoot AliPhysics; do
    pushd $d
      git pull origin
    popd
  done

  alibuild/aliDoctor AliPhysics || DOCTOR_ERROR=$?
  alibuild/aliBuild -j ${JOBS:-`nproc`} --reference-sources $MIRROR build AliPhysics || BUILD_ERROR=$?
  sleep 10
done
