#!/bin/bash
# A simple script which keeps building using the latest aliBuild,
# alidist and AliRoot / AliPhysics.
# Notice this will do an incremental build, not a full build, so it
# really to catch errors earlier.

MIRROR=${MIRROR:-/build/mirror}
PACKAGE=${PACKAGE:-AliPhysics}

pushd alidist
  ALIDIST_REF=`git rev-parse --verify HEAD`
popd
ali-bot/set-github-status -c alisw/alidist@$ALIDIST_REF -s build/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}/pending

while true; do
  for d in $(find . -maxdepth 2 -name .git -exec dirname {} \;); do
    pushd $d
      git pull origin
    popd
  done
  DOCTOR_ERROR=""
  BUILD_ERROR=""

  alibuild/aliDoctor ${ALIBUILD_DEFAULTS:+--defaults $ALIBUILD_DEFAULTS} $PACKAGE || DOCTOR_ERROR=$?
  if [[ $DOCTOR_ERROR != '' ]]; then
    ali-bot/set-github-status -c alisw/alidist@$ALIDIST_REF -s doctor/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}/error
  else
    ali-bot/set-github-status -c alisw/alidist@$ALIDIST_REF -s doctor/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}/success
  fi
  alibuild/aliBuild -j ${JOBS:-`nproc`}                                    \
                       ${ALIBUILD_DEFAULTS:+--defaults $ALIBUILD_DEFAULTS} \
                       --reference-sources $MIRROR                         \
                       build $PACKAGE || BUILD_ERROR=$?
  if [[ $BUILD_ERROR != '' ]]; then
    ali-bot/set-github-status -c alisw/alidist@$ALIDIST_REF -s build/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}/error
  else
    ali-bot/set-github-status -c alisw/alidist@$ALIDIST_REF -s build/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}/success
  fi
  sleep ${DELAY:-10}
done
