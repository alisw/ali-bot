#!/bin/bash -x
# This is the inner loop of continuous-builder.sh. This script builds one pull
# request for one repository. Which repo is checked depends on the environment
# variables passed to this script; they are set in continuous-builder.sh from
# environment definitions in repo-config/.
#
# Some functions used here are defined in continuous-builder.sh.

. build-helpers.sh
get_config

host_id=$(echo "$MESOS_EXECUTOR_ID" |
            sed -ne 's#^thermos-\([a-z]*\)-\([a-z]*\)-\([a-z0-9_-]*\)-\([0-9]*\)\(-[0-9a-f]*\)\{5\}$#build/\1/\2/\3/\4#p')
: "${host_id:=$(hostname --fqdn)}"

# Update all PRs in the queue with their number before we start building.
echo "$HASHES" | tail -n "+$((BUILD_SEQ + 1))" | cat -n | while read -r ahead btype num hash envf; do
  # Only report progress for a PR if it's never been built before.
  if [ "$btype" = untested ]; then
    # Run this in a subshell as report_pr_errors uses $PR_REPO but we don't want
    # to overwrite the outer for loop's variables, as they are needed for the
    # subsequent build.
    pr_repo=$(cd .. && source_env_files "$envf" && echo "$PR_REPO") &&
      # The --pr here will override the one specified in report_pr_errors.
      report_pr_errors --pr "$pr_repo#$num@$hash" --pending -m "Build queued ($ahead ahead) on $host_id"
  fi
done

if [ "$BUILD_TYPE" = untested ]; then
  # Set a status on GitHub showing the build start time, but only if this is
  # the first build! Rebuilds should only set the final success/failure.
  report_pr_errors --pending -m "Building since $(date +'%Y-%m-%d %H:%M %Z') on $host_id"
fi

if [ -z "$PR_REPO" ]; then
  echo 'No PR_REPO given; skipping' >&2
  exit 1
fi

# A few common environment variables when reporting status to analytics.
# In analytics we use screenviews to indicate different states of the
# processing and events to indicate all the things we would consider as
# fatal in a non deamon process but that here simply make us go to the
# next step.
echo "ALIBUILD_O2_FORCE_GPU: $ALIBUILD_O2_FORCE_GPU"
echo "AMDAPPSDKROOT: $AMDAPPSDKROOT"
echo "CMAKE_PREFIX_PATH: $CMAKE_PREFIX_PATH"
ALIBOT_ANALYTICS_USER_UUID=$(hostname -s)-$WORKER_INDEX${CI_NAME:+-$CI_NAME}
export ALIBOT_ANALYTICS_USER_UUID
ALIBOT_ANALYTICS_ARCHITECTURE=${CONTAINER_IMAGE%-builder:latest}_$(uname -m)
export ALIBOT_ANALYTICS_ARCHITECTURE=${ALIBOT_ANALYTICS_ARCHITECTURE##*/}
export ALIBOT_ANALYTICS_APP_NAME=continuous-builder.sh

# These variables are used by the report_state function
TIME_STARTED=$(date -u +%s)
CI_HASH=$(cd "$(dirname "$0")" && git rev-parse HEAD)

: "${MIRROR:=/build/mirror}" "${PACKAGE:=AliPhysics}" "${PR_REPO_CHECKOUT:=$(basename "$PR_REPO")}"

# This is the check name. If CHECK_NAME is in the environment, use it. Otherwise
# default to, e.g., build/AliRoot/release (build/<Package>/<Defaults>)
: "${CHECK_NAME:=build/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}}"

# Worker index, zero-based. Set to 0 if unset (i.e. when not running on Aurora)
: "${WORKER_INDEX:=0}"

# Get dependency development packages
echo "$DEVEL_PKGS" | while read -r gh_url branch checkout_name; do
  : "${checkout_name:=$(basename "$gh_url")}"
  if [ -d "$checkout_name" ]; then
    reset_git_repository "$checkout_name"
  else
    git clone "https://github.com/$gh_url" ${branch:+--branch "$branch"} "$checkout_name"
  fi
done

# Remove logs older than 5 days
find separate_logs/ -type f -mtime +5 -delete || true
find separate_logs/ -type d -empty -delete || true

# Run preliminary cleanup command
aliBuild clean ${DEBUG:+--debug}

LAST_PR=$PR_NUMBER
LAST_PR_OK=

# We are looping over several build hashes here. We will have one log per build.
mkdir -p "separate_logs/$(date -u +%Y%m%d-%H%M%S)-$PR_NUMBER-$PR_HASH"

report_state pr_processing

if pushd "$PR_REPO_CHECKOUT"; then
  git config --add remote.origin.fetch '+refs/pull/*/head:refs/remotes/origin/pr/*'
  # Only fetch destination branch for PRs (for merging), and the PR we are checking now
  short_timeout git fetch origin "+$PR_BRANCH:refs/remotes/origin/$PR_BRANCH"
  [[ "$PR_NUMBER" =~ ^[0-9]*$ ]] && short_timeout git fetch origin "+pull/$PR_NUMBER/head"
  git reset --hard "origin/$PR_BRANCH"  # reset to branch target of PRs
  git clean -fxd
  old_size=$(du -sm . | cut -f1)
  base_hash=$(git rev-parse --verify HEAD)  # reference upstream hash

  if ! git merge --no-edit "$PR_HASH"; then
    # clean up in case the merge fails
    git reset --hard HEAD
    git clean -fxd
    # We do not want to kill the system is github is not working
    # so we ignore the result code for now
    short_timeout set-github-status ${SILENT:+-n} -c "$PR_REPO@$PR_HASH" -s "$CHECK_NAME/error" -m 'Cannot merge PR into test area' ||
      short_timeout report-analytics exception --desc 'set-github-status fail on cannot merge'
    exit 1
  fi

  if [ $(($(du -sm . | cut -f1) - old_size)) -gt "${MAX_DIFF_SIZE:-5}" ]; then
    # We do not want to kill the system is github is not working
    # so we ignore the result code for now
    short_timeout set-github-status ${SILENT:+-n} -c "$PR_REPO@$PR_HASH" -s "$CHECK_NAME/error" -m 'Diff too big. Rejecting.' ||
      short_timeout report-analytics exception --desc 'set-github-status fail on merge too big'
    report_pr_errors -m 'Your pull request exceeded the allowed size. If you need to commit large files, [have a look here](http://alisw.github.io/git-advanced/#how-to-use-large-data-files-for-analysis).' ||
      short_timeout report-analytics exception --desc 'report-pr-errors fail on merge diff too big'
    exit 1
  fi

  popd
fi

if ! clean_env short_timeout aliDoctor ${ALIBUILD_DEFAULTS:+--defaults $ALIBUILD_DEFAULTS} "$PACKAGE"; then
  # We do not want to kill the system is github is not working
  # so we ignore the result code for now
  short_timeout set-github-status ${SILENT:+-n} -c "$PR_REPO@$PR_HASH" -s "$CHECK_NAME/error" -m 'aliDoctor error' ||
    short_timeout report-analytics exception --desc 'set-github-status fail on aliDoctor error'
  # If doctor fails, we can move on to the next PR, since we know it will not work.
  # We do not report aliDoctor being ok, because that's really a granted.
  exit 1
fi

# Each round we delete the "latest" symlink, to avoid reporting errors
# from a previous one. In any case they will be recreated if needed when
# we build.
mkdir -p sw/BUILD
find sw/BUILD/ -maxdepth 1 -name '*latest*' -delete
# Delete coverage files from one run to the next to avoid
# reporting them twice under erroneous circumstances
find sw/BUILD/ -maxdepth 4 -name coverage.info -delete

# Ensure build names do not clash across different PR jobs (O2-373)
BUILD_IDENTIFIER=${NO_ASSUME_CONSISTENT_EXTERNALS:+${PR_NUMBER//-/_}}
: "${BUILD_IDENTIFIER:=${CHECK_NAME//\//_}}"

# If remote store is set, make sure we can resolve it.
# if not it means we should probably restart the builder.
if [ -n "$REMOTE_STORE" ]; then
  ping -c1 "$(echo "$REMOTE_STORE" | cut -d/ -f3)"
fi

FETCH_REPOS="$(aliBuild build --help | grep fetch-repos || true)"

if ALIBUILD_HEAD_HASH=$PR_HASH ALIBUILD_BASE_HASH=$base_hash \
                     clean_env long_timeout aliBuild -j "${JOBS:-$(nproc)}" \
                     ${FETCH_REPOS:+--fetch-repos}                       \
                     ${ALIBUILD_DEFAULTS:+--defaults $ALIBUILD_DEFAULTS} \
                     -z "$BUILD_IDENTIFIER"                              \
                     ${MIRROR:+--reference-sources $MIRROR}              \
                     ${REMOTE_STORE:+--remote-store $REMOTE_STORE}       \
                     ${DEBUG:+--debug}                                   \
                     build "$PACKAGE"
then
  # We do not want to kill the system is github is not working
  # so we ignore the result code for now
  if [ $((PR_NUMBER + 0)) = "$PR_NUMBER" ]; then
    # This is a PR. Use the error function (with --success) to still provide logs
    report_pr_errors --success
  else
    # This is a branch
    short_timeout set-github-status ${SILENT:+-n} -c "$PR_REPO@$PR_HASH" -s "$CHECK_NAME/success"
  fi ||
    short_timeout report-analytics exception --desc 'report-pr-errors fail on build success'
  LAST_PR_OK=1
else
  # We do not want to kill the system if GitHub is not working
  # so we ignore the result code for now
  report_pr_errors ${DONT_USE_COMMENTS:+--no-comments} ||
    short_timeout report-analytics exception --desc 'report-pr-errors fail on build error'
  LAST_PR_OK=0
fi

# Run post-build cleanup command
aliBuild clean ${DEBUG:+--debug}

# Look for any code coverage file for the given commit and push
# it to codecov.io
COVERAGE_SOURCES=$PWD/$PR_REPO_CHECKOUT
COVERAGE_INFO_DIR=$(find sw/BUILD/ -maxdepth 4 -name coverage.info | head -1 | xargs dirname || true)
if [ -n "$COVERAGE_INFO_DIR" ] && pushd "$COVERAGE_INFO_DIR"; then
  COVERAGE_COMMIT_HASH=$PR_HASH
  if [ "$COVERAGE_COMMIT_HASH" = 0 ]; then
    COVERAGE_COMMIT_HASH=$base_hash
  fi
  # If not a number, it's the branch name
  if ! [[ $PR_NUMBER =~ ^[0-9]+$ ]]; then
    unset PR_NUMBER
  fi
  short_timeout bash <(curl --max-time 600 -s https://codecov.io/bash) \
                -R "$COVERAGE_SOURCES"      \
                -f coverage.info            \
                -C "$COVERAGE_COMMIT_HASH"  \
                ${PR_BRANCH:+-B $PR_BRANCH} \
                ${PR_NUMBER:+-P $PR_NUMBER} || true
  popd
fi

report_state pr_processing_done
