#!/bin/bash -x
# This is the inner loop of continuous-builder.sh. This script builds one pull
# request for one repository. Which repo is checked depends on the environment
# variables passed to this script; they are set in continuous-builder.sh from
# environment definitions in repo-config/.
#
# Some functions used here are defined in continuous-builder.sh.

. build-helpers.sh
get_config

ensure_vars CI_NAME CHECK_NAME PR_REPO PR_BRANCH PACKAGE ALIBUILD_DEFAULTS
export ALIBUILD_O2_TESTS
: "${WORKERS_POOL_SIZE:=1}" "${WORKER_INDEX:=0}" "${PR_REPO_CHECKOUT:=$(basename "$PR_REPO")}"
[ -d /build/mirror ] && : "${MIRROR:=/build/mirror}"

# This is the check name. If CHECK_NAME is in the environment, use it. Otherwise
# default to, e.g., build/AliRoot/release (build/<Package>/<Defaults>)
: "${CHECK_NAME:=build/$PACKAGE/$ALIBUILD_DEFAULTS}"

host_id=$(echo "$MESOS_EXECUTOR_ID" |
            sed -ne 's#^\(thermos-\)\?\([a-z]*\)-\([a-z]*\)-\([a-z0-9_-]*\)-\([0-9]*\)\(-[0-9a-f]*\)\{5\}$#\2/\4/\5#p')
: "${host_id:=$(hostname -f)}"

# Update all PRs in the queue with their number before we start building.
echo "$HASHES" | tail -n "+$((BUILD_SEQ + 1))" | cat -n | while read -r ahead btype num hash envf; do
  # Only report progress for a PR if it's never been built before.
  if [ "$btype" = untested ]; then (
    # Run this in a subshell as report_pr_errors uses $PR_REPO but we don't want
    # to overwrite the outer for loop's variables, as they are needed for the
    # subsequent build.
    cd ..
    source_env_files "$envf"
    PR_NUMBER=$num PR_HASH=$hash report_pr_errors --pending -m "Queued ($ahead ahead) on $host_id"
  ); fi
done

if [ "$BUILD_TYPE" = untested ]; then
  # Set a status on GitHub showing the build start time, but only if this is
  # the first build! Rebuilds should only set the final success/failure.
  report_pr_errors --pending -m "Building since $(date +'%Y-%m-%d %H:%M %Z') on $host_id"
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
ALIBOT_ANALYTICS_ARCHITECTURE=${CUR_CONTAINER}_$(uname -m)
export ALIBOT_ANALYTICS_USER_UUID ALIBOT_ANALYTICS_ARCHITECTURE
export ALIBOT_ANALYTICS_APP_NAME=continuous-builder.sh

# Get dependency development packages
if [ -n "$DEVEL_PKGS" ]; then
  echo "$DEVEL_PKGS" | while read -r gh_url branch checkout_name; do
    : "${checkout_name:=$(basename "$gh_url")}"
    if [ -d "$checkout_name" ]; then
      reset_git_repository "$checkout_name"
    else
      git clone "https://github.com/$gh_url" ${branch:+--branch "$branch"} "$checkout_name"
    fi
  done
fi

# Remove logs older than 5 days
find separate_logs/ -type f -mtime +5 -delete || true
find separate_logs/ -type d -empty -delete || true

# Run preliminary cleanup command
aliBuild clean ${DEBUG:+--debug}

# We are looping over several build hashes here. We will have one log per build.
mkdir -p "separate_logs/$(date -u +%Y%m%d-%H%M%S)-$PR_NUMBER-$PR_HASH"

report_state pr_processing

# Fetch the PR's changes to the git repository.
if pushd "$PR_REPO_CHECKOUT"; then
  git config --add remote.origin.fetch '+refs/pull/*/head:refs/remotes/origin/pr/*'
  # Only fetch destination branch for PRs (for merging), and the PR we are checking now
  short_timeout git fetch origin "+$PR_BRANCH:refs/remotes/origin/$PR_BRANCH"
  if is_numeric "$PR_NUMBER"; then
    short_timeout git fetch origin "+pull/$PR_NUMBER/head"
  fi
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
    short_timeout set-github-status ${SILENT:+-n} -c "$PR_REPO@$PR_HASH" -s "$CHECK_NAME/error" -m 'Please resolve merge conflicts' ||
      short_timeout report-analytics exception --desc 'set-github-status fail on cannot merge'
    exit 1
  fi

  if [ $(($(du -sm . | cut -f1) - old_size)) -gt "${MAX_DIFF_SIZE:-5}" ]; then
    # We do not want to kill the system is github is not working
    # so we ignore the result code for now
    short_timeout set-github-status ${SILENT:+-n} -c "$PR_REPO@$PR_HASH" -s "$CHECK_NAME/error" -m 'PR too big. Rejecting.' ||
      short_timeout report-analytics exception --desc 'set-github-status fail on merge too big'
    report_pr_errors -m 'Your pull request exceeded the allowed size. If you need to commit large files, [have a look here](http://alisw.github.io/git-advanced/#how-to-use-large-data-files-for-analysis).' ||
      short_timeout report-analytics exception --desc 'report-pr-errors fail on merge diff too big'
    exit 1
  fi

  popd
fi

if ! clean_env short_timeout aliDoctor --defaults "$ALIBUILD_DEFAULTS" "$PACKAGE"; then
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

# Only publish packages to remote store when we build the master branch. For
# PRs, PR_NUMBER will be numeric; in that case, only write to the regular
# read-only store. We can't compare against 'master' here as 'dev' is the
# "master branch" for O2.
if ! is_numeric "$PR_NUMBER"; then
  REMOTE_STORE=$BRANCH_REMOTE_STORE
fi

# Ensure build names do not clash across different PR jobs (O2-373)
build_identifier=${NO_ASSUME_CONSISTENT_EXTERNALS:+${PR_NUMBER//-/_}}
: "${build_identifier:=${CHECK_NAME//\//_}}"

if ALIBUILD_HEAD_HASH=$PR_HASH ALIBUILD_BASE_HASH=$base_hash             \
                     clean_env long_timeout aliBuild                     \
                     -j "${JOBS:-$(nproc)}" -z "$build_identifier"       \
                     --defaults "$ALIBUILD_DEFAULTS"                     \
                     ${MIRROR:+--reference-sources $MIRROR}              \
                     ${REMOTE_STORE:+--remote-store $REMOTE_STORE}       \
                     --fetch-repos ${DEBUG:+--debug} build "$PACKAGE"
then
  if is_numeric "$PR_NUMBER"; then
    # This is a PR. Use the error function (with --success) to still provide logs
    report_pr_errors --success
  else
    # This is a branch
    short_timeout set-github-status ${SILENT:+-n} -c "$PR_REPO@$PR_HASH" -s "$CHECK_NAME/success"
  fi ||
    short_timeout report-analytics exception --desc 'report-pr-errors fail on build success'
  PR_OK=1
else
  report_pr_errors ${DONT_USE_COMMENTS:+--no-comments} ||
    short_timeout report-analytics exception --desc 'report-pr-errors fail on build error'
  PR_OK=0
fi

# Run post-build cleanup command
aliBuild clean ${DEBUG:+--debug}

(
  # Look for any code coverage file for the given commit and push it to
  # codecov.io. Run in a subshell because we might unset PR_NUMBER, which
  # report_state (below) needs.
  coverage_sources=$PWD/$PR_REPO_CHECKOUT
  coverage_info_dir=$(find sw/BUILD/ -maxdepth 4 -name coverage.info -prune -printf %h)
  if [ -n "$coverage_info_dir" ] && cd "$coverage_info_dir"; then
    # If not a number, it's the branch name -- in that case, we don't want to
    # pass -P to codecov.
    is_numeric "$PR_NUMBER" || unset PR_NUMBER
    short_timeout bash <(curl --max-time 600 -s https://codecov.io/bash)  \
                  -R "$coverage_sources" -f coverage.info -C "$PR_HASH"   \
                  ${PR_BRANCH:+-B $PR_BRANCH} ${PR_NUMBER:+-P $PR_NUMBER} ||
      true
  fi
)

report_state pr_processing_done
