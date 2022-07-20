#!/bin/bash -x
# -*- sh-basic-offset: 2 -*-
# A simple script which keeps building using the latest aliBuild, alidist and
# AliRoot / AliPhysics. Notice this will do an incremental build, not a full
# build, so it really to catch errors earlier.

. build-helpers.sh

if [ "$1" != --skip-setup ]; then
  if [ -r ~/.continuous-builder ]; then
    . ~/.continuous-builder
  fi

  ensure_vars GITHUB_TOKEN GITLAB_USER GITLAB_PASS AWS_ACCESS_KEY_ID \
              AWS_SECRET_ACCESS_KEY INFLUXDB_WRITE_URL ALIBOT_ANALYTICS_ID \
              MONALISA_HOST MONALISA_PORT MESOS_ROLE CONTAINER_IMAGE \
              WORKER_INDEX WORKERS_POOL_SIZE
  # This can be empty or unspecified (in which case it defaults to empty).
  export ALIBOT_CONFIG_SUFFIX

  # Disable aliBuild analytics prompt
  mkdir -p ~/.config/alibuild
  touch ~/.config/alibuild/disable-analytics

  # Set up common global environment
  # Mesos DNSes
  : "${MESOS_DNS:=alimesos01.cern.ch,alimesos02.cern.ch,alimesos03.cern.ch}"
  export MESOS_DNS
  # Explicitly set UTF-8 support (Python needs it!)
  export {LANG{,UAGE},LC_{CTYPE,NUMERIC,TIME,COLLATE,MONETARY,PAPER,MESSAGES,NAME,ADDRESS,TELEPHONE,MEASUREMENT,IDENTIFICATION,ALL}}=en_US.UTF-8

  # GitLab credentials for private ALICE repositories
  printf 'protocol=https\nhost=gitlab.cern.ch\nusername=%s\npassword=%s\n' "$GITLAB_USER" "$GITLAB_PASS" |
    git credential-store --file ~/.git-creds store
  git config --global credential.helper 'store --file ~/.git-creds'

  # Git user setup
  git config --global user.name alibuild
  git config --global user.email alibuild@cern.ch

  # This turns a container image (e.g. alisw/slc8-gpu-builder:latest) into a
  # short, simple name like slc8-gpu that we use for the .env directories.
  CUR_CONTAINER=${CONTAINER_IMAGE#*/}
  CUR_CONTAINER=${CUR_CONTAINER%-builder:*}
  ARCHITECTURE=${CUR_CONTAINER%%-*}_$(uname -m | tr _ -)
  ARCHITECTURE=${ARCHITECTURE/#cs/slc}
  export CUR_CONTAINER ARCHITECTURE

  # On MacOS, the default ulimit for open files is 256. This is too low for git
  # when fetching some large repositories (e.g. O2, Clang).
  [ "$(ulimit -n)" -ge 10240 ] || ulimit -n 10240
fi

# Get updates to ali-bot, or clone it if it's the first time.
# This is for *.env files. These should always be taken from ali-bot@master,
# irrespective of the *installed* ali-bot version required by each repo.
TIMEOUT=$(get_config_value timeout "${TIMEOUT:-600}") \
       reset_git_repository ali-bot https://github.com/alisw/ali-bot

# Generate example of force-hashes file. This is used to override what to check for testing
if ! [ -e force-hashes ]; then
  cat > force-hashes <<EOF
# Override what to build using this file.
# Lines are of the form:
# BUILD_TYPE (PR_NUMBER|BRANCH_NAME) PR_HASH ENV_NAME
# Where:
# - BUILD_TYPE: one of: untested, failed, succeeded
# - ENV_NAME: the basename without ".env" of the .env file to source
EOF
fi

# Get a list of PRs to build -- force-hashes overrides list-branch-pr.
HASHES=$(grep -Eve '^[[:blank:]]*(#|$)' force-hashes || true)
if [ -z "$HASHES" ]; then
  get_config
  HASHES=$(list-branch-pr)
fi

if [ -n "$HASHES" ]; then
  # Loop through PRs we can build if there are any.
  echo "$HASHES" | cat -n | while read -r BUILD_SEQ BUILD_TYPE PR_NUMBER PR_HASH env_name; do (
    # Run iterations in a subshell so environment variables are not kept
    # across potentially different repos. This is an issue as env files are
    # allowed to define arbitrary variables that other files (or the defaults
    # file) might not override. Note that we are in a subshell here due to the
    # "list-branch-pr | while read" pipeline.

    # Setup environment
    # Skip this build if the .env file doesn't exist any more.
    source_env_files "$env_name" || exit

    # Make a directory for this repo's dependencies so they don't conflict
    # with other repos'
    mkdir -p "$env_name"
    cd "$env_name" || exit 10

    # Allow overriding the ali-bot/alibuild version to install -- this is useful
    # for testing changes to those with a few workers before deploying widely.
    pipinst "$(get_config_value install-alibot   "$INSTALL_ALIBOT")"   || exit 1
    pipinst "$(get_config_value install-alibuild "$INSTALL_ALIBUILD")" || exit 1

    # Run the build
    . build-loop.sh || :

    cd ..
    # Delete builds older than 2 days, then keep deleting until we've got at
    # least 100 GiB of free disk space.
    # We can't write metrics to stdout and pipe, as aliBuild's doClean also
    # writes to stdout.
    cleanup.py -o cleanup-metrics.txt -t 2 -f 100 "$MESOS_ROLE" "$CUR_CONTAINER"
    while read -r env_name duration_sec num_deleted_symlinks \
               bytes_freed bytes_free_before
    do (
      # Avoid inheriting variables from the previous build, in case sourcing
      # fails. These will be set by the CI environments we source below.
      unset CHECK_NAME PR_REPO
      . "ali-bot/ci/repo-config/$MESOS_ROLE/$CUR_CONTAINER/$env_name.env"
      # Push available space before cleanup as kib_avail (so we see how badly
      # the disk space ran out before cleanup). Available space after cleanup
      # can be calculated as kib_avail + kib_freed_approx.
      influxdb_push cleanup "host=$(hostname -s)" \
                    "os=$(uname -s | tr '[:upper:]' '[:lower:]')" \
                    "checkname=${CHECK_NAME:?}" "repo=${PR_REPO:?}" \
                    -- "duration_sec=${duration_sec:?}" \
                    "num_symlinks_deleted=${num_deleted_symlinks:?}" \
                    "kib_freed_approx=$((bytes_freed / 1024))" \
                    "kib_avail=$((bytes_free_before / 1024))"
    ); done < cleanup-metrics.txt

    # Something inside this subshell is reading stdin, which causes some hashes
    # from above to be ignored. It shouldn't, so redirect from /dev/null.
  ) < /dev/null; done
else
  # If we're idling, wait a while to conserve GitHub API requests.
  sleep "$(get_config_value timeout "${TIMEOUT:-600}")"
fi

# Re-exec ourselves. This lets us update pick up updates to this script, e.g.
# when upgraded by pip.
exec "$0" --skip-setup
