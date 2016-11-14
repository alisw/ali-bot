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
  for d in $(find . -maxdepth 2 -name .git -exec dirname {} \; | grep -v ali-bot); do
    pushd $d
      git pull origin
    popd
  done

  if [[ "$PR_REPO" != "" ]]; then
    HASHES=`ali-bot/list-branch-pr --show-main-branch ${TRUSTED_USERS:+--trusted $TRUSTED_USERS} $PR_REPO@$PR_BRANCH`
  else
    HASHES="0@0"
  fi
  for pr_id in $HASHES; do
    DOCTOR_ERROR=""
    BUILD_ERROR=""
    pr_number=${pr_id%@*}
    pr_hash=${pr_id#*@}
    if [[ "$PR_REPO" != "" ]]; then
      pushd `basename $PR_REPO`
        CANNOT_MERGE=0
        git reset --hard origin/$PR_BRANCH
        git config --add remote.origin.fetch "+refs/pull/*/head:refs/remotes/origin/pr/*"
        git fetch origin
        git clean -fxd
        git rev-parse --verify HEAD
        git merge $pr_hash || CANNOT_MERGE=1
        PR_REF=$pr_hash
      popd
      if [[ $CANNOT_MERGE == 1 ]]; then
        # We do not want to kill the system is github is not working
        # so we ignore the result code for now
        ali-bot/set-github-status -c ${PR_REPO:-alisw/alidist}@${PR_REF:-$ALIDIST_REF} -s build/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}/error -m "Cannot merge PR into test area" || true
        continue
      fi
    fi

    alibuild/aliDoctor ${ALIBUILD_DEFAULTS:+--defaults $ALIBUILD_DEFAULTS} $PACKAGE || DOCTOR_ERROR=$?
    STATUS_REF=${PR_REPO:-alisw/alidist}@${PR_REF:-$ALIDIST_REF}
    if [[ $DOCTOR_ERROR != '' ]]; then
      # We do not want to kill the system is github is not working
      # so we ignore the result code for now
      ali-bot/set-github-status -c ${STATUS_REF} -s doctor/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}/error || true
    else
      # We do not want to kill the system is github is not working
      # so we ignore the result code for now
      ali-bot/set-github-status -c ${STATUS_REF} -s doctor/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}/success || true
    fi
    # Each round we delete the "latest" symlink, to avoid reporting errors
    # from a previous one. In any case they will be recreated if needed when
    # we build.
    mkdir -p sw/BUILD
    find sw/BUILD/ -maxdepth 1 -name "*latest*" -delete

    GITHUB_TOKEN= alibuild/aliBuild -j ${JOBS:-`nproc`}                       \
                         ${ALIBUILD_DEFAULTS:+--defaults $ALIBUILD_DEFAULTS}  \
                         -z $(echo ${pr_number} | tr - _)                     \
                         --reference-sources $MIRROR                          \
                         ${REMOTE_STORE:+--remote-store $REMOTE_STORE}        \
                         ${DEBUG:+--debug}                                    \
                         build $PACKAGE || BUILD_ERROR=$?
    STATE_CONTEXT=build/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}
    if [[ $BUILD_ERROR != '' ]]; then
      # We do not want to kill the system is github is not working
      # so we ignore the result code for now
      ali-bot/set-github-status -c ${STATUS_REF} -s $STATE_CONTEXT/error || true
      # We do not want to kill the system is github is not working
      # so we ignore the result code for now
      ali-bot/report-pr-errors --default $BUILD_SUFFIX                                              \
                               --pr "${PR_REPO:-alisw/alidist}#${pr_id}" -s $STATE_CONTEXT || true
    else
      # We do not want to kill the system is github is not working
      # so we ignore the result code for now
      ali-bot/set-github-status -c ${STATUS_REF} -s $STATE_CONTEXT/success || true
    fi
  done
  sleep ${DELAY:-600} || true
done
