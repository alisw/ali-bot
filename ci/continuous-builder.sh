#!/bin/bash
# A simple script which keeps building using the latest aliBuild,
# alidist and AliRoot / AliPhysics.
# Notice this will do an incremental build, not a full build, so it
# really to catch errors earlier.

# A few common environment variables when reporting status to analytics.
# In analytics we use screenviews to indicate different states of the
# processing and events to indicate all the things we would consider as
# fatal in a non deamon process but that here simly make us go to the
# next step.
export ALIBOT_ANALYTICS_ID=$ALIBOT_ANALYTICS_ID
export ALIBOT_ANALYTICS_USER_UUID=`hostname -s`-$WORKER_INDEX${CI_NAME:+-$CI_NAME}
# Hardcode for now
export ALIBOT_ANALYTICS_ARCHITECTURE=slc7_x86-64
export ALIBOT_ANALYTICS_APP_NAME="continuous-builder.sh"

MIRROR=${MIRROR:-/build/mirror}
PACKAGE=${PACKAGE:-AliPhysics}

pushd alidist
  ALIDIST_REF=`git rev-parse --verify HEAD`
popd
set-github-status -c alisw/alidist@$ALIDIST_REF -s build/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}/pending

function report_state() {
  CURRENT_STATE=$1
  # Push some metric about being up and running to monalisa.
  report-metric-monalisa --metric-path github-pr-checker.${CI_NAME:+$CI_NAME}_Nodes/$ALIBOT_ANALYTICS_USER_UUID \
                         --metric-name state                                                                    \
                         --metric-value $CURRENT_STATE
  report-analytics screenview --cd $CURRENT_STATE
}

report_state started

while true; do
  report_state looping
  for d in $(find . -maxdepth 2 -name .git -exec dirname {} \; | grep -v ali-bot); do
    pushd $d
      git pull origin
    popd
  done

  if [[ "$PR_REPO" != "" ]]; then
    HASHES=`list-branch-pr --show-main-branch ${CHECK_NAME:+--check-name $CHECK_NAME} ${TRUST_COLLABORATORS:+--trust-collaborators} ${TRUSTED_USERS:+--trusted $TRUSTED_USERS} $PR_REPO@$PR_BRANCH ${WORKERS_POOL_SIZE:+--workers-pool-size $WORKERS_POOL_SIZE} ${WORKER_INDEX:+--worker-index $WORKER_INDEX} || true`
  else
    HASHES="0@0"
  fi

  for pr_id in $HASHES; do
    report_state pr_processing
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
        OLD_SIZE=`du --exclude=.git -sb . | awk '{print $1}'`
        git rev-parse --verify HEAD
        git merge $pr_hash || CANNOT_MERGE=1
        NEW_SIZE=`du --exclude=.git -sb . | awk '{print $1}'`
        PR_REF=$pr_hash
      popd
      if [[ $CANNOT_MERGE == 1 ]]; then
        # We do not want to kill the system is github is not working
        # so we ignore the result code for now
        set-github-status -c ${PR_REPO:-alisw/alidist}@${PR_REF:-$ALIDIST_REF} -s build/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}/error -m "Cannot merge PR into test area" || true
        continue
      fi
      if [[ $(($NEW_SIZE - $OLD_SIZE)) -gt ${MAX_DIFF_SIZE:-4000000} ]]; then
        # We do not want to kill the system is github is not working
        # so we ignore the result code for now
        set-github-status -c ${PR_REPO:-alisw/alidist}@${PR_REF:-$ALIDIST_REF} -s build/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}/error -m "Diff to big. Rejecting." || true
        continue
      fi
    fi

    alibuild/aliDoctor ${ALIBUILD_DEFAULTS:+--defaults $ALIBUILD_DEFAULTS} $PACKAGE || DOCTOR_ERROR=$?
    STATUS_REF=${PR_REPO:-alisw/alidist}@${PR_REF:-$ALIDIST_REF}
    if [[ $DOCTOR_ERROR != '' ]]; then
      # We do not want to kill the system is github is not working
      # so we ignore the result code for now
      set-github-status -c ${STATUS_REF} -s build/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}/error -m 'aliDoctor error' || true
      # If doctor fails, we can move on to the next PR, since we know it will not work.
      # We do not report aliDoctor being ok, because that's really a granted.
      continue
    fi
    # Each round we delete the "latest" symlink, to avoid reporting errors
    # from a previous one. In any case they will be recreated if needed when
    # we build.
    mkdir -p sw/BUILD
    find sw/BUILD/ -maxdepth 1 -name "*latest*" -delete
    GITHUB_TOKEN= alibuild/aliBuild -j ${JOBS:-`nproc`}                       \
                         ${ALIBUILD_DEFAULTS:+--defaults $ALIBUILD_DEFAULTS}  \
                         ${NO_ASSUME_CONSISTENT_EXTERNALS:+-z $(echo ${pr_number} | tr - _)} \
                         --reference-sources $MIRROR                          \
                         ${REMOTE_STORE:+--remote-store $REMOTE_STORE}        \
                         ${DEBUG:+--debug}                                    \
                         build $PACKAGE || BUILD_ERROR=$?
    STATE_CONTEXT=build/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}
    if [[ $BUILD_ERROR != '' ]]; then
      # We do not want to kill the system is github is not working
      # so we ignore the result code for now
      report-pr-errors --default $BUILD_SUFFIX                                              \
                       --pr "${PR_REPO:-alisw/alidist}#${pr_id}" -s $STATE_CONTEXT || true
    else
      # We do not want to kill the system is github is not working
      # so we ignore the result code for now
      set-github-status -c ${STATUS_REF} -s $STATE_CONTEXT/success || true
    fi
    report_state pr_processing_done
  done
  report_state looping_done
  if [[ $ONESHOT = true ]]; then
    echo "Called with ONESHOT=true. Exiting."
    exit 0
  fi
  sleep ${DELAY:-600} || true
done
