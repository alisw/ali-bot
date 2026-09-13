#!/bin/bash -x
# This file contains functions used by continuous-builder.sh and build-loop.sh.
# It is sourced on every iteration, so functions defined here can be overridden
# while the builder is running.

function influxdb_push () {
  set +x
  # Usage: influxdb_push TABLE TAG=V TAG=V -- FIELD=V FIELD=V
  # Turn args into an InfluxDB string like "table,tag=v,tag=v field=v,field=v time".
  local data=$1; shift
  while [ $# -gt 0 ]; do
    case $1 in
      --) data="$data $2"; shift 2;;
      *)  data="$data,$1"; shift;;
    esac
  done
  data="$data $(date +%s)000000000"
  case "$INFLUXDB_WRITE_URL" in
    '') ;;
    # If INFLUXDB_WRITE_URL starts with insecure_https://, then strip
    # "insecure_" and send the --insecure/-k option to curl.
    insecure_*)
      curl -fSs --max-time 20 -XPOST --data-binary "$data" -k "${INFLUXDB_WRITE_URL#insecure_}" || :;;
    *)
      curl -fSs --max-time 20 -XPOST --data-binary "$data" "$INFLUXDB_WRITE_URL" || :;;
  esac
  set -x
}

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
  # shellcheck disable=SC2153  # PR_NUMBER comes from the environment
  influxdb_push prcheck "repo=$PR_REPO" "checkname=$CHECK_NAME" \
                "worker=$CHECK_NAME/$WORKER_INDEX/$WORKERS_POOL_SIZE" \
                ${NUM_BASE_COMMITS:+"num_base_commits=$NUM_BASE_COMMITS"} \
                "prev_build=$BUILD_TYPE" \
                -- "host=\"$(hostname -s)\"" "state=\"$current_state\"" \
                "prid=\"$PR_NUMBER\"" ${prtime:+prtime=$prtime} ${PR_OK:+prok=$PR_OK} \
                ${WAITING_SINCE:+waittime=$((time_now - WAITING_SINCE))} \
                ${HAVE_JALIEN_TOKEN:+have_jalien_token=$HAVE_JALIEN_TOKEN}

  # ...and the same numbers to Mimir, when the caller has wired OTLP up. Guarded
  # on the URL rather than on the check name, so a job opts in by exporting
  # OTLP_METRICS_URL in its round-setup and every other builder is untouched.
  #
  # Only the numeric fields go: otlp-push.py turns each FIELD into its own
  # Prometheus series, and a series whose value is a string is not a thing.
  # state/host/prid stay InfluxDB-only; state becomes a TAG here instead, which
  # is what lets a dashboard separate a finished round from a starting one.
  #
  # These are GAUGES, not counters -- a dashboard must not rate() them. prtime is
  # already a duration in seconds, so it is read directly rather than differenced.
  if [ -n "$OTLP_METRICS_URL" ]; then
    otlp-push.py prcheck "repo=$PR_REPO" "checkname=$CHECK_NAME" \
                 "arch=${ARCHITECTURE:-unknown}" "state=$current_state" \
                 "host=$(hostname -s)" \
                 -- ${prtime:+"prtime=$prtime"} ${PR_OK:+"prok=$PR_OK"} \
                 ${WAITING_SINCE:+"waittime=$((time_now - WAITING_SINCE))"} ||
      echo "report_state: OTLP push failed, continuing" >&2
  fi

  # Push to Google Analytics if configured
  if [ -n "$ALIBOT_ANALYTICS_ID" ] && [ -n "$prtime" ]; then
    short_timeout report-analytics timing --utc 'PR Building' --utv time --utt $((prtime * 1000)) --utl "$CHECK_NAME/$WORKER_INDEX"
  fi
}

function clean_env () {
  # This function calls its arguments with access tokens removed from the environment.
  # The X509_USER_* vars might not apply if building inside a container, so
  # remove them too. They shouldn't be used by the build anyway.
  GITLAB_USER='' GITLAB_PASS='' GITHUB_TOKEN='' INFLUXDB_WRITE_URL='' CODECOV_TOKEN='' \
    X509_USER_CERT='' X509_USER_KEY='' "$@"
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
}

function reset_git_repository () {
  # Reset the specified git repository to its original, remote state.
  #
  # Called with no ref, this behaves exactly as it always has: fetch whatever
  # branch the checkout is on and hard-reset to it, leaving a detached HEAD
  # alone. Every caller except DEVEL_PKGS uses that form.
  #
  # Called with --branch REF, the ref is now honoured on an EXISTING checkout
  # too, not only when cloning. It used to be a clone-time argument only, so
  # pointing a check at a different alidist did nothing at all on any worker
  # that had already built once: the directory was there, the argument was
  # ignored, and the checkout kept resetting to the branch it was created on.
  # Silently -- which is the worst way for a pin to fail.
  #
  # REF may be a branch, a tag, or a pull request written as pull/N/head. The
  # last needs an explicit refspec: refs/pull/* is outside the default one, so
  # neither clone nor fetch brings it down by itself.
  #
  # The result is always a detached HEAD, so nothing later drags the checkout
  # back to a branch -- and the ref is re-fetched every round, so a pinned PR
  # follows its own pushes rather than freezing at whatever it pointed to when
  # the work area was made.
  local repodir=$1
  shift   # $@ now contains args for git clone

  local ref='' prev='' arg
  for arg in "$@"; do
    case $prev in
      --branch|-b) ref=$arg ;;
    esac
    prev=$arg
  done

  # pull/N/head and refs/pull/N/head both mean pull request N.
  local pr_number='' src=$ref
  case $ref in
    pull/*/head|refs/pull/*/head)
      pr_number=${ref#refs/}
      pr_number=${pr_number#pull/}
      pr_number=${pr_number%/head}
      src=refs/pull/$pr_number/head
      ;;
  esac

  if pushd "$repodir"; then
    if [ -n "$ref" ]; then
      # One refspec covers all three cases: git resolves the source side on the
      # remote, so a branch, a tag and refs/pull/N/head all land in refs/pinned.
      short_timeout git fetch -f origin "+${src}:refs/pinned" &&
        git checkout -f --detach refs/pinned &&
        git clean -fxd
      popd || return 10
      return
    fi
    # The repo already exists.
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
  else
    # Directory doesn't exist or we can't read it; clone the repo from scratch.
    rm -rf "$repodir"
    if [ -n "$pr_number" ]; then
      # git clone --branch cannot take a PR ref, so clone plain and fetch it
      # afterwards. Branches and tags are left on the original path below, which
      # already handles both, rather than being rerouted through new code.
      local clone_args=() skip_next=
      for arg in "$@"; do
        if [ -n "$skip_next" ]; then skip_next=; continue; fi
        case $arg in
          --branch|-b) skip_next=1; continue ;;
        esac
        clone_args+=("$arg")
      done
      short_timeout git clone "${clone_args[@]}" "$repodir" &&
        ( cd "$repodir" &&
          short_timeout git fetch -f origin "+${src}:refs/pinned" &&
          git checkout -f --detach refs/pinned )
    else
      # Sometimes the clone gets stuck on large repos, so we need the timeout.
      short_timeout git clone "$@" "$repodir"
    fi
  fi
}

function build_type_to_status () {
  # Translate a build type from list-branch-pr into a GitHub check status.
  case "$1" in
    untested) echo pending;;
    failed) echo error;;
    succeeded) echo success;;
    *) echo "WARNING: unrecognised status $BUILD_TYPE, falling back to pending" >&2
       echo pending;;
  esac
}

function report_pr_errors () {
  # This is a wrapper for report-pr-errors with some default switches.
  local repo checkout_name extra_args=()
  # If we're not checking alidist, only include logs for the package we're
  # actually building in the HTML log. If we are checking alidist, all logs are
  # potentially relevant.
  if [ "$PR_REPO" != alisw/alidist ] && [ -n "$PACKAGE" ]; then
    extra_args+=(--main-package "$PACKAGE")
    # In $DEVEL_PKGS, the checkout name (third field) must be a valid package
    # name (otherwise aliBuild wouldn't recognise it as a development package).
    while read -r repo _ checkout_name; do
      [ "$repo" = alisw/alidist ] && continue
      extra_args+=(--main-package "${checkout_name:-$(basename "$repo")}")
    done <<< "$DEVEL_PKGS"
  fi
  short_timeout report-pr-errors ${SILENT:+--dry-run} --default "$BUILD_SUFFIX" \
                --pr "$PR_REPO#$PR_NUMBER@$PR_HASH" -s "$CHECK_NAME"            \
                --logs-dest s3://alice-build-logs.s3.cern.ch                    \
                --log-url https://ali-ci.cern.ch/alice-build-logs/              \
                "${extra_args[@]}" "$@"
}

function source_env_files () {
  local _envf env_name=$1 base=ali-bot/ci/repo-config
  # Go through the fallback order of the *.env files and source them, if
  # present. The exit status of this function will be the result of the last
  # iteration, i.e. nonzero if $env_name.env doesn't exist or failed.
  for _envf in "$base/DEFAULTS.env" \
                 "$base/$MESOS_ROLE/DEFAULTS.env" \
                 "$base/$MESOS_ROLE/$CUR_CONTAINER$ALIBOT_CONFIG_SUFFIX/DEFAULTS.env" \
                 "$base/$MESOS_ROLE/$CUR_CONTAINER$ALIBOT_CONFIG_SUFFIX/$env_name.env"
  do
    # Don't check the sourced file here. The .env files are checked separately.
    # shellcheck source=/dev/null
    [ -e "$_envf" ] && . "$_envf"
  done
}

function expand_date_spec () {
  # Expand %(date) and %(date +FMT) in $1 for the day $2 days ago (default 0).
  #
  # Shared by list-release-tags, which uses the result to FIND a tag, and by
  # build-one.sh, which uses it to PARSE the tag it was given. One expansion in
  # one place: a release check names its tags once, and the two cannot drift.
  #
  # %(date) is shorthand for %(date +%Y%m%d), the form every dated tag in
  # alidist uses.
  local spec=$1 day=${2:-0} fmt rest out='' value
  while [ -n "$spec" ]; do
    case $spec in
      *'%(date'*)
        out=$out${spec%%'%(date'*}
        rest=${spec#*'%(date'}
        case $rest in
          ' +'*) fmt=${rest#' +'}; fmt=${fmt%%')'*}; rest=${rest#*')'} ;;
          ')'*)  fmt='%Y%m%d';    rest=${rest#')'} ;;
          *) echo "malformed %(date ...) in: $1" >&2; return 1 ;;
        esac
        # GNU and BSD date disagree; these scripts run on the macOS builders too.
        if date -d "-$day day" +%Y > /dev/null 2>&1; then
          value=$(date -d "-$day day" "+$fmt")
        else
          value=$(date -v-"$day"d "+$fmt")
        fi
        out=$out$value
        spec=$rest
        ;;
      *) out=$out$spec; spec= ;;
    esac
  done
  printf '%s' "$out"
}

function is_numeric () {
  # A glob rather than $(($1 + 0)): arithmetic expansion on a release tag prints
  # "O2PDPSuite-daily-20260825-0000_TEST: value too great for base" to stderr
  # before returning false, and every release build log carries that line.
  case $1 in ''|*[!0-9]*) return 1;; *) return 0;; esac
}

function modtime () {
  # Get the file modification time, as a UNIX timestamp, in an OS-agnostic way.
  case $(uname -s) in
    Darwin) stat -t '%s' -f '%m' "$@";;
    Linux)  stat -c '%Y' "$@";;
    *) echo "unknown platform: $(uname -s)" >&2; return 1;;
  esac
}

function ensure_vars () {
  # Make sure variables are defined, and export them.
  for var in "$@"; do
    # The +x is needed to avoid leaking secrets when running with
    # bash -ex
    if [ -z ${!var+x} ]; then
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
function short_timeout () { general_timeout "$TIMEOUT" "$@"; }
function long_timeout () { general_timeout "$LONG_TIMEOUT" "$@"; }
function general_timeout () {
  local ret=0 short_cmd
  $timeout_exec -s9 "$@" || ret=$?
  # 124 if command times out; 137 if command is killed (including by timeout itself)
  if [ $ret -eq 124 ] || [ $ret -eq 137 ]; then
    # BASH_{SOURCE,LINENO}[0] is where we're being called from, which is inside
    # short_timeout or long_timeout (and thus not interesting).
    # BASH_{SOURCE,LINENO}[1] is where *those* functions are being called from.
    case $3 in
      -*|'') short_cmd=$2 ;;
      *) short_cmd="$2 $3" ;;
    esac
    influxdb_push ci_timeout_overrun \
                  "host=$(hostname -s)" "cmd=$short_cmd" "timeout_retcode=$ret" \
                  "called_from=$(basename "${BASH_SOURCE[1]}"):${BASH_LINENO[1]}" \
                  -- "timeout=$1"
  fi
  return $ret
}
