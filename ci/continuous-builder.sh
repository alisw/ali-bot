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
echo ALIBUILD_O2_FORCE_GPU: $ALIBUILD_O2_FORCE_GPU
echo AMDAPPSDKROOT: $AMDAPPSDKROOT
echo CMAKE_PREFIX_PATH: $CMAKE_PREFIX_PATH
export ALIBOT_ANALYTICS_ID=$ALIBOT_ANALYTICS_ID
export ALIBOT_ANALYTICS_USER_UUID=`hostname -s`-$WORKER_INDEX${CI_NAME:+-$CI_NAME}
# Hardcode for now
export ALIBOT_ANALYTICS_ARCHITECTURE=slc7_x86-64
export ALIBOT_ANALYTICS_APP_NAME="continuous-builder.sh"

# Mesos DNSes
: ${MESOS_DNS:=alimesos01.cern.ch,alimesos02.cern.ch,alimesos03.cern.ch}
export MESOS_DNS

TIME_STARTED=$(date -u +%s)
CI_HASH=$(cd "$(dirname "$0")" && git rev-parse HEAD)

# Timeout between calls of list-branch-pr
LIST_BRANCH_PR_TIMEOUT=
if [[ $ONESHOT == true ]]; then
  LIST_BRANCH_PR_TIMEOUT=1
  DELAY=1
fi

# timeout vs. gtimeout (macOS with Homebrew)
TIMEOUT_EXEC=timeout
type timeout > /dev/null 2>&1 || TIMEOUT_EXEC=gtimeout

MIRROR=${MIRROR:-/build/mirror}
PACKAGE=${PACKAGE:-AliPhysics}
TIMEOUT_CMD="$TIMEOUT_EXEC -s9 ${TIMEOUT:-600}"
LONG_TIMEOUT_CMD="$TIMEOUT_EXEC -s9 ${LONG_TIMEOUT:-36000}"
LAST_PR=
PR_REPO_CHECKOUT=${PR_REPO_CHECKOUT:-$(basename "$PR_REPO")}

# If INFLUXDB_WRITE_URL starts with insecure_https://, then strip "insecure" and
# set the proper option to curl
INFLUX_INSECURE=
[[ $INFLUXDB_WRITE_URL == insecure_https:* ]] && { INFLUX_INSECURE=-k; INFLUXDB_WRITE_URL=${INFLUXDB_WRITE_URL:9}; }

# Last time `git gc` was run
LAST_GIT_GC=0

# This is the check name. If CHECK_NAME is in the environment, use it. Otherwise
# default to, e.g., build/AliRoot/release (build/<Package>/<Defaults>)
CHECK_NAME=${CHECK_NAME:=build/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}}

# Worker index, zero-based. Set to 0 if unset (i.e. when not running on Aurora)
WORKER_INDEX=${WORKER_INDEX:-0}

pushd alidist
  ALIDIST_REF=`git rev-parse --verify HEAD`
popd
$TIMEOUT_CMD set-github-status -c alisw/alidist@$ALIDIST_REF -s $CHECK_NAME/pending

# Generate example of force-hashes file. This is used to override what to check for testing
if [[ ! -e force-hashes ]]; then
  cat > force-hashes <<EOF
# Example (this is a comment):
# pr_number@hash
# You can also use:
# branch_name@hash
EOF
fi

function report_state() {
  CURRENT_STATE=$1
  # Push some metric about being up and running to Monalisa
  $TIMEOUT_CMD report-metric-monalisa --metric-path github-pr-checker.${CI_NAME:+$CI_NAME}_Nodes/$ALIBOT_ANALYTICS_USER_UUID \
                                      --metric-name state                                                                    \
                                      --metric-value $CURRENT_STATE
  $TIMEOUT_CMD report-analytics screenview --cd $CURRENT_STATE
  # Push to InfluxDB if configured
  if [[ $INFLUXDB_WRITE_URL ]]; then
    TIME_NOW=$(date -u +%s)
    TIME_NOW_NS=$((TIME_NOW*1000000000))
    PRTIME=
    [[ $CURRENT_STATE == pr_processing ]] && TIME_PR_STARTED=$TIME_NOW
    [[ $CURRENT_STATE == pr_processing_done ]] && PRTIME=",prtime=$((TIME_NOW-TIME_PR_STARTED))"
    DATA="prcheck,checkname=$CHECK_NAME/$WORKER_INDEX host=\"$(hostname -s)\",state=\"$CURRENT_STATE\",cihash=\"$CI_HASH\",uptime=$((TIME_NOW-TIME_STARTED))${PRTIME}${LAST_PR:+,prid=\"$LAST_PR\"}${LAST_PR_OK:+,prok=$LAST_PR_OK} $TIME_NOW_NS"
    curl $INFLUX_INSECURE --max-time 20 -XPOST "$INFLUXDB_WRITE_URL" --data-binary "$DATA" || true
  fi
}

# Explicitly set UTF-8 support (Python needs it!)
export LANG="en_US.UTF-8"
export LANGUAGE="en_US.UTF-8"
export LC_CTYPE="en_US.UTF-8"
export LC_NUMERIC="en_US.UTF-8"
export LC_TIME="en_US.UTF-8"
export LC_COLLATE="en_US.UTF-8"
export LC_MONETARY="en_US.UTF-8"
export LC_MESSAGES="en_US.UTF-8"
export LC_PAPER="en_US.UTF-8"
export LC_NAME="en_US.UTF-8"
export LC_ADDRESS="en_US.UTF-8"
export LC_TELEPHONE="en_US.UTF-8"
export LC_MEASUREMENT="en_US.UTF-8"
export LC_IDENTIFICATION="en_US.UTF-8"
export LC_ALL="en_US.UTF-8"

report_state started

mkdir -p config

while true; do
  report_state looping
  # Allow overriding a number of variables by fly, so that we can change the
  # behavior of the job without restarting it.
  # This comes handy when scaling up / down a job, so that we do not quit the
  # currently running workers simply to adapt to the new ensamble.
  [ -f config/workers-pool-size ] && WORKERS_POOL_SIZE=`cat config/workers-pool-size 2>/dev/null | head -n 1`
  [ -f config/worker-index ] && WORKER_INDEX=`cat config/worker-index 2>/dev/null | head -n 1`
  [ -f config/debug ] && DEBUG=`cat config/debug 2>/dev/null | head -n 1`
  [ -f config/profile ] && PROFILE=`cat config/profile 2>/dev/null | head -n 1`
  [ -f config/jobs ] && JOBS=`cat config/jobs 2>/dev/null | head -n 1`
  [ -f config/timeout ] && TIMEOUT=`cat config/jobs 2>/dev/null | head -n 1`
  [ -f config/long-timeout ] && LONG_TIMEOUT=`cat config/jobs 2>/dev/null | head -n 1`
  [ -f config/silent ] && SILENT=`cat config/silent 2>/dev/null | head -n 1`
  # In case the files are gone, unset some of the variables so that we can
  # revert the state.
  [ ! -f config/silent ] && unset SILENT
  [ ! -f config/debug ] && unset DEBUG
  [ ! -f config/profile ] && unset PROFILE

  # Run preliminary cleanup command
  aliBuild clean ${DEBUG:+--debug}

  # Update and cleanup all Git repositories (except ali-bot)
  for d in $(find . -maxdepth 2 -name .git -exec dirname {} \; | grep -v ali-bot); do
    pushd $d
      LOCAL_BRANCH=$(git rev-parse --abbrev-ref HEAD)
      if [[ $LOCAL_BRANCH != HEAD ]]; then
        # Cleanup first (if more than 4h passed after last gc)
        git update-ref -d refs/remotes/origin/pr/* || true
        if [[ $(( $(date -u +%s) - $LAST_GIT_GC )) -gt 14400 ]]; then
          git reflog expire --expire=now --all || true
          git gc --prune=now || true
          LAST_GIT_GC=$(date -u +%s)
        fi
        # Try to reset to corresponding remote branch (assume it's origin/<branch>)
        $TIMEOUT_CMD git fetch origin +$LOCAL_BRANCH:refs/remotes/origin/$LOCAL_BRANCH
        git reset --hard origin/$LOCAL_BRANCH
        git clean -fxd
      fi
    popd
  done

  # Remove logs older than 5 days
  find separate_logs/ -type f -mtime +5 -delete || true
  find separate_logs/ -type d -empty -delete || true

  if [[ "$PR_REPO" != "" ]]; then
    HASHES=$(cat force-hashes | grep -vE '^#' 2> /dev/null || true)
    if [[ ! $HASHES ]]; then
      HASHES=`$TIMEOUT_CMD list-branch-pr ${LIST_BRANCH_PR_TIMEOUT:+--timeout $LIST_BRANCH_PR_TIMEOUT} --show-main-branch --check-name $CHECK_NAME ${TRUST_COLLABORATORS:+--trust-collaborators} ${TRUSTED_USERS:+--trusted $TRUSTED_USERS} $PR_REPO@$PR_BRANCH ${WORKERS_POOL_SIZE:+--workers-pool-size $WORKERS_POOL_SIZE} ${WORKER_INDEX:+--worker-index $WORKER_INDEX} ${DELAY:+--max-wait $DELAY} || $TIMEOUT_CMD report-analytics exception --desc "list-branch-pr failed"`
    else
      echo "Note: using hashes from $PWD/force-hashes, here is the list:"
      cat $PWD/force-hashes
      echo
    fi
  else
    HASHES="0@0"
  fi

  if [ X$ONESHOT = Xtrue ]; then
    echo "Called with ONESHOT=true. Only one PR tested."
    HASHES=`echo $HASHES | head -n 1`
  fi

  for pr_id in $HASHES; do
    [ -f config/debug ] && DEBUG=`cat config/debug 2>/dev/null | head -n 1`
    [ -f config/profile ] && PROFILE=`cat config/profile 2>/dev/null | head -n 1`
    [ -f config/jobs ] && JOBS=`cat config/jobs 2>/dev/null | head -n 1`
    [ -f config/silent ] && SILENT=`cat config/silent 2>/dev/null | head -n 1`
    # In case the files are gone, unset some of the variables so that we can
    # revert the state.
    [ ! -f config/silent ] && unset SILENT
    [ ! -f config/debug ] && unset DEBUG
    [ ! -f config/profile ] && unset PROFILE

    DOCTOR_ERROR=""
    BUILD_ERROR=""
    pr_number=${pr_id%@*}
    pr_hash=${pr_id#*@}
    LAST_PR=$pr_number
    LAST_PR_OK=

    # We are looping over several build hashes here. We will have one log per build.
    SLOG_DIR="separate_logs/$(date -u +%Y%m%d-%H%M%S)-${pr_number}-${pr_hash}"
    mkdir -p "$SLOG_DIR"

    report_state pr_processing
    if [[ "$PR_REPO" != "" ]]; then
      pushd $PR_REPO_CHECKOUT
        CANNOT_MERGE=0
        git config --add remote.origin.fetch "+refs/pull/*/head:refs/remotes/origin/pr/*"
        # Only fetch destination branch for PRs (for merging), and the PR we are checking now
        $TIMEOUT_CMD git fetch origin +$PR_BRANCH:refs/remotes/origin/$PR_BRANCH
        [[ $pr_number =~ ^[0-9]*$ ]] && $TIMEOUT_CMD git fetch origin +pull/$pr_number/head
        git reset --hard origin/$PR_BRANCH  # reset to branch target of PRs
        git clean -fxd
        OLD_SIZE=`du -sm . | cut -f1`
        base_hash=$(git rev-parse --verify HEAD)  # reference upstream hash
        git merge --no-edit $pr_hash || CANNOT_MERGE=1
        # clean up in case the merge fails
        git reset --hard HEAD
        git clean -fxd
        NEW_SIZE=`du -sm . | cut -f1`
        PR_REF=$pr_hash
      popd
      if [[ $CANNOT_MERGE == 1 ]]; then
        # We do not want to kill the system is github is not working
        # so we ignore the result code for now
        $TIMEOUT_CMD set-github-status ${SILENT:+-n} -c ${PR_REPO:-alisw/alidist}@${PR_REF:-$ALIDIST_REF} -s $CHECK_NAME/error -m "Cannot merge PR into test area" || $TIMEOUT_CMD report-analytics exception --desc "set-github-status fail on cannot merge"
        continue
      fi
      if [[ $(($NEW_SIZE - $OLD_SIZE)) -gt ${MAX_DIFF_SIZE:-5} ]]; then
        # We do not want to kill the system is github is not working
        # so we ignore the result code for now
        $TIMEOUT_CMD set-github-status ${SILENT:+-n} -c ${PR_REPO:-alisw/alidist}@${PR_REF:-$ALIDIST_REF} -s $CHECK_NAME/error -m "Diff too big. Rejecting." || $TIMEOUT_CMD report-analytics exception --desc "set-github-status fail on merge too big"
        $TIMEOUT_CMD report-pr-errors --default $BUILD_SUFFIX                            \
                                      ${SILENT:+--dry-run}                               \
                                      --logs-dest s3://alice-build-logs.s3.cern.ch       \
                                      --log-url https://ali-ci.cern.ch/alice-build-logs/ \
                                      --pr "${PR_REPO:-alisw/alidist}#${pr_id}"          \
                                      -s $CHECK_NAME -m "Your pull request exceeded the allowed size. If you need to commit large files, [have a look here](http://alisw.github.io/git-advanced/#how-to-use-large-data-files-for-analysis)." || $TIMEOUT_CMD report-analytics exception --desc "report-pr-errors fail on merge diff too big"
        continue
      fi
    fi

    GITLAB_USER= GITLAB_PASS= GITHUB_TOKEN= INFLUXDB_WRITE_URL= CODECOV_TOKEN= \
    AWS_ACCESS_KEY_ID= AWS_SECRET_ACCESS_KEY=                                  \
    $TIMEOUT_CMD aliDoctor ${ALIBUILD_DEFAULTS:+--defaults $ALIBUILD_DEFAULTS} $PACKAGE || DOCTOR_ERROR=$?
    STATUS_REF=${PR_REPO:-alisw/alidist}@${PR_REF:-$ALIDIST_REF}
    if [[ $DOCTOR_ERROR != '' ]]; then
      # We do not want to kill the system is github is not working
      # so we ignore the result code for now
      $TIMEOUT_CMD set-github-status ${SILENT:+-n} -c ${STATUS_REF} -s $CHECK_NAME/error -m 'aliDoctor error' || $TIMEOUT_CMD report-analytics exception --desc "set-github-status fail on aliDoctor error"
      # If doctor fails, we can move on to the next PR, since we know it will not work.
      # We do not report aliDoctor being ok, because that's really a granted.
      continue
    fi
    # Each round we delete the "latest" symlink, to avoid reporting errors
    # from a previous one. In any case they will be recreated if needed when
    # we build.
    mkdir -p sw/BUILD
    find sw/BUILD/ -maxdepth 1 -name "*latest*" -delete
    # Delete coverage files from one run to the next to avoid
    # reporting them twice under erroneous circumstances
    find sw/BUILD/ -maxdepth 4 -name coverage.info -delete

    # GitLab credentials for private ALICE repositories
    printf "protocol=https\nhost=gitlab.cern.ch\nusername=$GITLAB_USER\npassword=$GITLAB_PASS\n" | \
    git credential-store --file ~/.git-creds store
    git config --global credential.helper "store --file ~/.git-creds"

    # Ensure build names do not clash across different PR jobs (O2-373)
    BUILD_IDENTIFIER=${NO_ASSUME_CONSISTENT_EXTERNALS:+$(echo ${pr_number} | tr - _)}
    [[ $BUILD_IDENTIFIER ]] || BUILD_IDENTIFIER=${CHECK_NAME//\//_}

    FETCH_REPOS="$(aliBuild build --help | grep fetch-repos || true)"
    ALIBUILD_HEAD_HASH=$pr_hash ALIBUILD_BASE_HASH=$base_hash                    \
    GITLAB_USER= GITLAB_PASS= GITHUB_TOKEN= INFLUXDB_WRITE_URL= CODECOV_TOKEN=   \
    AWS_ACCESS_KEY_ID= AWS_SECRET_ACCESS_KEY=                                    \
    $LONG_TIMEOUT_CMD                                                            \
    aliBuild -j ${JOBS:-`nproc`}                                                 \
             ${FETCH_REPOS:+--fetch-repos}                                       \
             ${ALIBUILD_DEFAULTS:+--defaults $ALIBUILD_DEFAULTS}                 \
             -z $BUILD_IDENTIFIER                                                \
             --reference-sources $MIRROR                                         \
             ${REMOTE_STORE:+--remote-store $REMOTE_STORE}                       \
             ${DEBUG:+--debug}                                                   \
             ${ALIBUILD_O2_FORCE_GPU:+--always-prefer-system}                    \
             build $PACKAGE || BUILD_ERROR=$?

    if [[ $BUILD_ERROR != '' ]]; then
      # We do not want to kill the system if GitHub is not working
      # so we ignore the result code for now
      $TIMEOUT_CMD report-pr-errors --default $BUILD_SUFFIX                            \
                                    ${SILENT:+--dry-run}                               \
                                    ${DONT_USE_COMMENTS:+--no-comments}                \
                                    --logs-dest s3://alice-build-logs.s3.cern.ch       \
                                    --log-url https://ali-ci.cern.ch/alice-build-logs/ \
                                    --pr "${PR_REPO:-alisw/alidist}#${pr_id}" -s $CHECK_NAME || $TIMEOUT_CMD report-analytics exception --desc "report-pr-errors fail on build error"
    else
      # We do not want to kill the system is github is not working
      # so we ignore the result code for now
      if [[ $(( $pr_number + 0 )) == $pr_number ]]; then
        # This is a PR. Use the error function (with --success) to still provide logs
        $TIMEOUT_CMD report-pr-errors --default $BUILD_SUFFIX                            \
                                      ${SILENT:+--dry-run}                               \
                                      --success                                          \
                                      --logs-dest s3://alice-build-logs.s3.cern.ch       \
                                      --log-url https://ali-ci.cern.ch/alice-build-logs/ \
                                      --pr "${PR_REPO:-alisw/alidist}#${pr_id}" -s $CHECK_NAME || $TIMEOUT_CMD report-analytics exception --desc "report-pr-errors fail on build success"
      else
        # This is a branch
        $TIMEOUT_CMD set-github-status ${SILENT:+-n} -c ${STATUS_REF} -s $CHECK_NAME/success || $TIMEOUT_CMD report-analytics exception --desc "set-github-status fail on build success"
      fi
    fi
    [[ $BUILD_ERROR ]] && LAST_PR_OK=0 || LAST_PR_OK=1

    # Run post-build cleanup command
    aliBuild clean ${DEBUG:+--debug}

    # Look for any code coverage file for the given commit and push
    # it to codecov.io
    COVERAGE_SOURCES=$PWD/${PR_REPO_CHECKOUT:-$(basename $PR_REPO)}
    COVERAGE_INFO_DIR=$(find sw/BUILD/ -maxdepth 4 -name coverage.info | head -1 | xargs dirname || true)
    if [[ ${COVERAGE_INFO_DIR} ]]; then
      pushd ${COVERAGE_INFO_DIR}
        COVERAGE_COMMIT_HASH=${pr_hash}
        if [[ $COVERAGE_COMMIT_HASH == 0 ]]; then
          COVERAGE_COMMIT_HASH=${base_hash}
        fi
        # If not a number, it's the branch name
        re='^[0-9]+$'
        if ! [[ $pr_number =~ $re ]] ; then
          unset pr_number
        fi
        $TIMEOUT_CMD bash <(curl --max-time 600 -s https://codecov.io/bash)                 \
                                                -R $COVERAGE_SOURCES                        \
                                                -f coverage.info                            \
                                                -C ${COVERAGE_COMMIT_HASH}                  \
                                                ${PR_BRANCH:+-B $PR_BRANCH}                 \
                                                ${pr_number:+-P $pr_number} || true
      popd
    fi
    report_state pr_processing_done
  done  # end processing a single PR

  report_state looping_done
  # Mark the fact we have run at least once.
  mkdir -p state
  touch state/ready
  if [[ $ONESHOT = true ]]; then
    echo "Called with ONESHOT=true. Exiting."
    exit 0
  fi
done
