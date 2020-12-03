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

  if ! [ -d ali-bot ]; then
    # This is for *.env files. These should always be taken from ali-bot@master,
    # irrespective of the *installed* ali-bot version required by each repo.
    git clone https://github.com/alisw/ali-bot
  fi

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
  export CUR_CONTAINER=${CUR_CONTAINER%-builder:*}
fi

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
    . build-loop.sh
  ); done &&
    # If the loop succeeded, remove force-hashes so we don't keep building the
    # same PRs forever.
    rm -f force-hashes
else
  # If we're idling, wait a while to conserve GitHub API requests.
  sleep "$(get_config_value timeout "${TIMEOUT:-600}")"
fi

# Get updates to ali-bot
TIMEOUT=$(get_config_value timeout "${TIMEOUT:-600}") reset_git_repository ali-bot

# Run CI builder under screen if possible and we're not already there. This is
# for the macOS builders.
if [ "$MESOS_ROLE" != macos ] || [ -n "$STY" ] || ! type screen > /dev/null; then
  exec "$0" --skip-setup
else
  exec screen -dmS "ci_$WORKER_INDEX" "$0" --skip-setup
fi
