#!/bin/bash -x
# This file contains functions used by continuous-builder.sh and build-loop.sh.
# It is sourced on every iteration, so functions defined here can be overridden
# while the builder is running.

function report_state () {
  local current_state=$1
  # Push some metric about being up and running to Monalisa
  short_timeout report-metric-monalisa --metric-path "github-pr-checker.${CI_NAME}_Nodes/$ALIBOT_ANALYTICS_USER_UUID" --metric-name state --metric-value "$current_state"
  short_timeout report-analytics screenview --cd "$current_state"

  # Calculate PR statistics
  local time_now prtime=
  time_now=$(date -u +%s)
  case "$current_state" in
    pr_processing) TIME_PR_STARTED=$time_now;;
    pr_processing_done) prtime=$((time_now - TIME_PR_STARTED));;
  esac

  # Push to InfluxDB if configured
  if [ -n "$INFLUXDB_WRITE_URL" ]; then
    printf 'prcheck,checkname=%s host="%s",state="%s",prid="%s"%s %s'                 \
           "$CHECK_NAME/$WORKER_INDEX" "$(hostname -s)" "$current_state" "$PR_NUMBER" \
           "${prtime:+,prtime=$prtime}${PR_OK:+,prok=$PR_OK}" $((time_now * 10**9))   |
      case "$INFLUXDB_WRITE_URL" in
        # If INFLUXDB_WRITE_URL starts with insecure_https://, then strip
        # "insecure_" and send the --insecure/-k option to curl.
        insecure_*)
          curl --max-time 20 -XPOST "${INFLUXDB_WRITE_URL#insecure_}" -k --data-binary @- || true;;
        *)
          curl --max-time 20 -XPOST "$INFLUXDB_WRITE_URL" --data-binary @- || true;;
      esac
  fi

  # Push to Google Analytics if configured
  if [ -n "$ALIBOT_ANALYTICS_ID" ] && [ -n "$prtime" ]; then
    short_timeout report-analytics timing --utc 'PR Building' --utv time --utt $((prtime * 1000)) --utl "$CHECK_NAME/$WORKER_INDEX"
  fi
}

function clean_env () {
  # This function calls its arguments with access tokens removed from the environment.
  GITLAB_USER='' GITLAB_PASS='' GITHUB_TOKEN='' INFLUXDB_WRITE_URL='' CODECOV_TOKEN='' "$@"
}

function pipinst () {
  # Sometimes pip gets stuck when cloning the ali-bot or alibuild repos. In
  # that case: time out, skip and try again later.
  short_timeout pip install --upgrade --upgrade-strategy only-if-needed "git+https://github.com/$1"
}

# Allow overriding a number of variables by fly, so that we can change the
# behavior of the job without restarting it.
# This comes handy when scaling up / down a job, so that we do not quit the
# currently running workers simply to adapt to the new ensemble.
function get_config_value () {
  # Looks for a config/ directory here or in the parent dir and prints the
  # contents of the file called $1 in the closest config/ found. If no config/
  # is found, prints $2.
  # This function is called in the start directory or in a repo-specific
  # subdirectory, so we only need to look one level up at most.
  head -1 "config/$1" 2>/dev/null ||
    head -1 "../config/$1" 2>/dev/null ||
    echo "$2"
}

function get_config () {
  # Read configuration files and set the appropriate env variables. This allows
  # overriding some critical variables on-the-fly by writing files in config/.
  WORKERS_POOL_SIZE=$(get_config_value workers-pool-size "$WORKERS_POOL_SIZE")
  WORKER_INDEX=$(get_config_value worker-index "$WORKER_INDEX")
  TIMEOUT=$(get_config_value timeout "${TIMEOUT:-600}")
  LONG_TIMEOUT=$(get_config_value long-timeout "${LONG_TIMEOUT:-36000}")
  # If the files have been deleted in the meantime, this will set the variables
  # to the empty string.
  SILENT=$(get_config_value silent)
  DEBUG=$(get_config_value debug)
}

function reset_git_repository () {
  # Reset the specified git repository to its original, remote state.
  pushd "$1" || return 10
  local local_branch
  local_branch=$(git rev-parse --abbrev-ref HEAD)
  if [ "$local_branch" != HEAD ]; then
    # Cleanup first
    if [ -d .git/refs/remotes/origin/pr ]; then
      find .git/refs/remotes/origin/pr | sed 's|^\.git/||' | xargs -n 1 git update-ref -d
    fi
    # Try to reset to corresponding remote branch (assume it's origin/<branch>)
    short_timeout git fetch origin "+$local_branch:refs/remotes/origin/$local_branch"
    git reset --hard "origin/$local_branch"
    git clean -fxd
  fi
  popd || return 10
}

function report_pr_errors () {
  # This is a wrapper for report-pr-errors with some default switches.
  short_timeout report-pr-errors ${SILENT:+--dry-run} --default "$BUILD_SUFFIX" \
                --pr "$PR_REPO#$PR_NUMBER@$PR_HASH" -s "$CHECK_NAME"            \
                --logs-dest s3://alice-build-logs.s3.cern.ch                    \
                --log-url https://ali-ci.cern.ch/alice-build-logs/              \
                "$@"
}

function source_env_files () {
  local _envf env_name=$1 base=ali-bot/ci/repo-config
  # Go through the fallback order of the *.env files and source them, if
  # present. The exit status of this function will be the result of the last
  # iteration, i.e. nonzero if $env_name.env doesn't exist or failed.
  for _envf in "$base/DEFAULTS.env" \
                 "$base/$MESOS_ROLE/DEFAULTS.env" \
                 "$base/$MESOS_ROLE/$CUR_CONTAINER/DEFAULTS.env" \
                 "$base/$MESOS_ROLE/$CUR_CONTAINER/$env_name.env"
  do
    [ -e "$_envf" ] && . "$_envf"
  done
}

function is_numeric () {
  [ $(($1 + 0)) = "$1" ]
}

function ensure_vars () {
  # Make sure variables are defined, and export them.
  for var in "$@"; do
    if [ -z "${!var}" ]; then
      echo "$(basename "$0"): error: required variable $var not defined!" >&2
      exit 1
    else
      export "${var?}"
    fi
  done
}

# timeout vs. gtimeout (macOS with Homebrew)
timeout_exec=timeout
type $timeout_exec > /dev/null 2>&1 || timeout_exec=gtimeout
function short_timeout () { $timeout_exec -s9 "$TIMEOUT" "$@"; }
function long_timeout () { $timeout_exec -s9 "$LONG_TIMEOUT" "$@"; }
