#!/bin/bash -x
# -*- sh-basic-offset: 2 -*-
# Report how much work is queued for one pool of CI workers, without building
# anything.
#
# Run this as a single service per (MESOS_ROLE, CUR_CONTAINER) pair, alongside
# the builders. It exists because the builders cannot measure this themselves:
# each of them only ever sees its own shard of the PRs (list-branch-pr's
# should_process()), and it always finds *something* to do, since it falls back
# to rebuilding an already-tested PR when its shard is empty. So a busy builder
# tells us nothing about whether there is real work waiting.
#
# Metrics are pushed to InfluxDB as the "ci_queue" measurement, one point per
# check, every $QUEUE_METRICS_INTERVAL seconds.

. build-helpers.sh

# A deliberate near-copy of influxdb_push from build-helpers.sh. DO NOT refactor
# the two together: influxdb_push is on the builders' hot path and is left
# exactly as it is, so deploying this collector changes nothing about how they
# reach InfluxDB. This copy differs in two ways:
#
#   * it can send the credential in an Authorization header rather than
#     embedded in the URL, which is what lets it write through the
#     security-proxy (with INFLUXDB_WRITE_TOKEN unset it behaves identically,
#     so the script still runs by hand outside Nomad);
#   * it does not honour the "insecure_" prefix. The proxy verifies the
#     upstream properly, so there is nothing to skip.
function queue_metrics_push () {
  set +x   # never trace the URL or the token
  # Usage: queue_metrics_push TABLE TAG=V TAG=V -- FIELD=V FIELD=V
  local data=$1; shift
  while [ $# -gt 0 ]; do
    case $1 in
      --) data="$data $2"; shift 2;;
      *)  data="$data,$1"; shift;;
    esac
  done
  data="$data $(date +%s)000000000"
  # An empty array expands to no arguments at all, including under the bash 3.2
  # that ships with macOS.
  local auth=()
  if [ -n "$INFLUXDB_WRITE_TOKEN" ]; then
    auth=(-H "Authorization: Bearer $INFLUXDB_WRITE_TOKEN")
  fi
  case "$INFLUXDB_WRITE_URL" in
    '') ;;
    # Complain rather than POSTing to a URL starting with "insecure_https://"
    # and having the error swallowed below. The builders' URL does carry that
    # prefix, so this is the likely mistake when running the script by hand.
    insecure_*)
      echo "$(basename "$0"): error: INFLUXDB_WRITE_URL must not use the insecure_ prefix" >&2;;
    *)
      curl -fSs --max-time 20 -XPOST --data-binary "$data" "${auth[@]}" "$INFLUXDB_WRITE_URL" || :;;
  esac
  set -x
}

# --skip-setup: we are a re-exec of ourselves, or our caller did the setup.
# --once:       report one round and exit, instead of looping forever. Use this
#               when the caller holds credentials that expire (e.g. gate tokens
#               from a credential broker) and must re-resolve them per round: our
#               own re-exec keeps the environment, so it would carry a stale one.
skip_setup='' once=''
for arg in "$@"; do
  case $arg in
    --skip-setup) skip_setup=true ;;
    --once) once=true ;;
    *) echo "$0: error: unrecognised argument: $arg" >&2; exit 1 ;;
  esac
done

if [ -z "$skip_setup" ]; then
  if [ -r ~/.continuous-builder ]; then
    # Tell ShellCheck not to check the sourced file here. Assume the .env files are fine.
    # shellcheck source=/dev/null
    . ~/.continuous-builder
  fi

  ensure_vars GITHUB_TOKEN INFLUXDB_WRITE_URL MESOS_ROLE
  # These can be empty or unspecified (in which case they default to empty).
  export ALIBOT_CONFIG_SUFFIX

  # Explicitly set UTF-8 support (Python needs it!)
  export {LANG{,UAGE},LC_{CTYPE,NUMERIC,TIME,COLLATE,MONETARY,PAPER,MESSAGES,NAME,ADDRESS,TELEPHONE,MEASUREMENT,IDENTIFICATION,ALL}}=en_US.UTF-8

  # Derive CUR_CONTAINER the same way continuous-builder.sh does, so that this
  # job can be given the same CONTAINER_IMAGE as the pool it is watching.
  if [ -z "$CUR_CONTAINER" ]; then
    ensure_vars CONTAINER_IMAGE
    CUR_CONTAINER=${CONTAINER_IMAGE##*/}
    CUR_CONTAINER=${CUR_CONTAINER%-builder:*}
  fi
  export CUR_CONTAINER

  python3 -m pip install --upgrade \
      "ali-bot[ci] @ git+https://github.com/${INSTALL_ALIBOT:-alisw/ali-bot@master}" ||
    exit 1
fi

# short_timeout/long_timeout need these; we have no *.env file to take them from
# until the checkout below exists.
: "${TIMEOUT:=300}" "${LONG_TIMEOUT:=600}"

# Get updates to ali-bot, or clone it if it's the first time. We need the *.env
# files, and they must be the ones from ali-bot@master, exactly like the
# builders use.
reset_git_repository ali-bot https://github.com/alisw/ali-bot

run_start_time=$(date +%s)

envdir=ali-bot/ci/repo-config/$MESOS_ROLE/$CUR_CONTAINER$ALIBOT_CONFIG_SUFFIX
if ! [ -d "$envdir" ]; then
  echo "$0: error: no such environment directory: $envdir" >&2
  exit 1
fi

# List the checks this pool is responsible for. We need these separately from
# the list-branch-pr output below, so that a check with an empty queue reports
# zero instead of dropping out of the metrics entirely -- "no data" and "no
# work queued" must not look the same to whatever consumes these metrics.
# Keep this a space-separated list: BSD awk (i.e. macOS) rejects literal
# newlines inside a -v assignment, so we cannot pass one name per line.
env_names=
for envf in "$envdir"/*.env; do
  env_name=$(basename "$envf" .env)
  [ "$env_name" = DEFAULTS ] || env_names="$env_names $env_name"
done

# WORKERS_POOL_SIZE=1 makes should_process() accept every PR, so we see the
# whole queue rather than one worker's shard. --no-status keeps us read-only:
# we must not interfere with the statuses the builders set.
if queue=$(WORKER_INDEX=0 WORKERS_POOL_SIZE=1 \
           short_timeout list-branch-pr --all-groups --no-status)
then
  poll_ok=1
else
  # An empty queue and a failed query both leave $queue empty, so we must go by
  # the exit code. Push nothing in the latter case: reporting "no work queued"
  # when GitHub is merely unreachable would tell anything scaling off these
  # metrics to stand the workers down during an outage.
  poll_ok=0
  echo "$0: warning: could not list pull requests; not reporting queue depth" >&2
fi

# Report whether we can see the queue at all, separately from how deep it is,
# so that a blind collector is distinguishable from an idle pool.
queue_metrics_push ci_queue_poll "host=$(hostname -s)" "role=$MESOS_ROLE" \
                   "container=$CUR_CONTAINER$ALIBOT_CONFIG_SUFFIX" -- "ok=$poll_ok"

# Aggregate per check: how many PRs in each state, and how long the oldest
# untested one has been waiting. Only untested PRs carry a meaningful
# waiting_since, which is why the age is reported for those alone.
[ "$poll_ok" = 1 ] && echo "$queue" |
  awk -F'\t' -v now="$(date -u +%s)" -v env_list="$env_names" '
    BEGIN {
      split(env_list, listed, " ")
      for (i in listed) if (listed[i] != "") envs[listed[i]] = 1
    }
    $4 != "" { envs[$4] = 1; count[$4 SUBSEP $1]++ }
    $1 == "untested" && $5 != "" {
      if (!($4 in oldest) || $5 + 0 < oldest[$4]) oldest[$4] = $5 + 0
    }
    END {
      for (env_name in envs)
        printf "%s\t%d\t%d\t%d\t%d\n", env_name,
               count[env_name SUBSEP "untested"],
               count[env_name SUBSEP "failed"],
               count[env_name SUBSEP "succeeded"],
               (env_name in oldest) ? now - oldest[env_name] : 0
    }' |
  while read -r env_name untested failed succeeded oldest_wait; do (
    set -e   # exit subshell = skip this check
    # Run in a subshell: the *.env files define arbitrary variables, and one
    # check's definitions must not leak into the next one's.
    source_env_files "$env_name"
    queue_metrics_push ci_queue "host=$(hostname -s)"                        \
                  "role=$MESOS_ROLE"                                    \
                  "container=$CUR_CONTAINER$ALIBOT_CONFIG_SUFFIX"       \
                  "checkname=${CHECK_NAME:?}" "repo=${PR_REPO:?}"       \
                  -- "untested=$untested" "failed=$failed"              \
                  "succeeded=$succeeded"                                \
                  "total=$((untested + failed + succeeded))"            \
                  "oldest_untested_wait_secs=$oldest_wait"
  ); done

# One round only: the caller owns the pacing, and presumably the credentials too.
[ -n "$once" ] && exit 0

# Wait out the rest of the interval, so we poll GitHub at a predictable rate
# whether or not the query above was slow.
run_duration=$(($(date +%s) - run_start_time))
interval=$(get_config_value queue-metrics-interval "${QUEUE_METRICS_INTERVAL:-300}")
if [ "$run_duration" -lt "$interval" ]; then
  sleep $((interval - run_duration)) || :
fi

# Re-exec ourselves. This lets us pick up updates to this script, e.g. when
# upgraded by pip.
exec "$0" --skip-setup
