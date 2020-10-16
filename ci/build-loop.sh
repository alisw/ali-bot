#!/bin/bash -x
# This is the inner loop of continuous-builder.sh. This script builds one pull
# request for one repository. Which repo is checked depends on the environment
# variables passed to this script; they are set in continuous-builder.sh from
# environment definitions in repo-config/.
#
# Some functions used here are defined in continuous-builder.sh.

get_config

report_state looping

# Run preliminary cleanup command
aliBuild clean ${DEBUG:+--debug}

# Update and cleanup all Git repositories (except ali-bot)
find . -mindepth 2 -maxdepth 2 -name .git | while read -r d; do
  [ "$d" = ./ali-bot/.git ] && continue
  reset_git_repository "$(dirname "$d")"
done

# Remove logs older than 5 days
find separate_logs/ -type f -mtime +5 -delete || true
find separate_logs/ -type d -empty -delete || true

if [ -z "$PR_REPO" ]; then
  echo 'No PR_REPO given; skipping' >&2
  exit 1
else
  HASHES=$(grep -vE '^[[:blank:]]*(#|$)' force-hashes 2>/dev/null || true)
  if [ -z "$HASHES" ]; then
    HASHES=$(short_timeout list-branch-pr --show-main-branch "$PR_REPO@$PR_BRANCH" \
                           --check-name "$CHECK_NAME" \
                           ${TRUST_COLLABORATORS:+--trust-collaborators} \
                           ${TRUSTED_USERS:+--trusted $TRUSTED_USERS} \
                           ${WORKERS_POOL_SIZE:+--workers-pool-size $WORKERS_POOL_SIZE} \
                           ${WORKER_INDEX:+--worker-index $WORKER_INDEX} \
                           ${DELAY:+--max-wait $DELAY}) ||
        short_timeout report-analytics exception --desc 'list-branch-pr failed'
  else
    echo "Note: using hashes from $PWD/force-hashes, here is the list:"
    cat $PWD/force-hashes
    echo
  fi
fi

  DOCTOR_ERROR=""
  BUILD_ERROR=""
for PR_ID in $HASHES; do
  . build-helpers.sh
  get_config

  pr_number=${PR_ID%@*}
  pr_hash=${PR_ID#*@}
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
      short_timeout git fetch origin "+$PR_BRANCH:refs/remotes/origin/$PR_BRANCH"
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
      short_timeout set-github-status ${SILENT:+-n} -c "${PR_REPO:-alisw/alidist}@${PR_REF:-$ALIDIST_REF}" -s "$CHECK_NAME/error" -m 'Cannot merge PR into test area' ||
        short_timeout report-analytics exception --desc 'set-github-status fail on cannot merge'
      continue
    fi
    if [[ $(($NEW_SIZE - $OLD_SIZE)) -gt ${MAX_DIFF_SIZE:-5} ]]; then
      # We do not want to kill the system is github is not working
      # so we ignore the result code for now
      short_timeout set-github-status ${SILENT:+-n} -c "${PR_REPO:-alisw/alidist}@${PR_REF:-$ALIDIST_REF}" -s "$CHECK_NAME/error" -m 'Diff too big. Rejecting.' ||
        short_timeout report-analytics exception --desc 'set-github-status fail on merge too big'
      report_pr_errors -m 'Your pull request exceeded the allowed size. If you need to commit large files, [have a look here](http://alisw.github.io/git-advanced/#how-to-use-large-data-files-for-analysis).' ||
        short_timeout report-analytics exception --desc 'report-pr-errors fail on merge diff too big'
      continue
    fi
  fi

  STATUS_REF=${PR_REPO:-alisw/alidist}@${PR_REF:-$ALIDIST_REF}
  if ! clean_env short_timeout aliDoctor ${ALIBUILD_DEFAULTS:+--defaults $ALIBUILD_DEFAULTS} "$PACKAGE"; then
    # We do not want to kill the system is github is not working
    # so we ignore the result code for now
    short_timeout set-github-status ${SILENT:+-n} -c "$STATUS_REF" -s "$CHECK_NAME/error" -m 'aliDoctor error' ||
      short_timeout report-analytics exception --desc 'set-github-status fail on aliDoctor error'
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

  # If remote store is set, make sure we can resolve it.
  # if not it means we should probably restart the builder.
  if [ ! X$REMOTE_STORE = X ]; then ping -c1 `echo $REMOTE_STORE | awk -F/ '{print $3}'`; fi

  FETCH_REPOS="$(aliBuild build --help | grep fetch-repos || true)"

  if ALIBUILD_HEAD_HASH=$pr_hash ALIBUILD_BASE_HASH=$base_hash \
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
    if [[ $(( $pr_number + 0 )) == $pr_number ]]; then
      # This is a PR. Use the error function (with --success) to still provide logs
      report_pr_errors --success
    else
      # This is a branch
      short_timeout set-github-status ${SILENT:+-n} -c "$STATUS_REF" -s "$CHECK_NAME/success"
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
  COVERAGE_SOURCES=$PWD/${PR_REPO_CHECKOUT:-$(basename $PR_REPO)}
  COVERAGE_INFO_DIR=$(find sw/BUILD/ -maxdepth 4 -name coverage.info | head -1 | xargs dirname || true)
  if [ -n "$COVERAGE_INFO_DIR" ] && pushd "$COVERAGE_INFO_DIR"; then
    COVERAGE_COMMIT_HASH=$pr_hash
    if [ "$COVERAGE_COMMIT_HASH" = 0 ]; then
      COVERAGE_COMMIT_HASH=$base_hash
    fi
    # If not a number, it's the branch name
    if ! [[ $pr_number =~ ^[0-9]+$ ]]; then
      unset pr_number
    fi
    short_timeout bash <(curl --max-time 600 -s https://codecov.io/bash) \
                  -R "$COVERAGE_SOURCES"      \
                  -f coverage.info            \
                  -C "$COVERAGE_COMMIT_HASH"  \
                  ${PR_BRANCH:+-B $PR_BRANCH} \
                  ${pr_number:+-P $pr_number} || true
    popd
  fi
  report_state pr_processing_done
done  # end processing a single PR

report_state looping_done
