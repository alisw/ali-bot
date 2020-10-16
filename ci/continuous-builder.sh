#!/bin/bash -x
# A simple script which keeps building using the latest aliBuild, alidist and
# AliRoot / AliPhysics. Notice this will do an incremental build, not a full
# build, so it really to catch errors earlier.

# timeout vs. gtimeout (macOS with Homebrew)
TIMEOUT_EXEC=timeout
type $TIMEOUT_EXEC > /dev/null 2>&1 || TIMEOUT_EXEC=gtimeout
function short_timeout () { $TIMEOUT_EXEC -s9 "$(get_config_value timeout "$TIMEOUT")" "$@"; }
function long_timeout () { $TIMEOUT_EXEC -s9 "$(get_config_value long-timeout "$LONG_TIMEOUT")" "$@"; }

. build-helpers.sh

# In one-repo-only mode, environment definition files aren't read, and we assume
# e.g. aurora has already given us the required environment for the one specific
# build we should do.
ONE_REPO_ONLY=
for arg in "$@"; do
  if [ "$arg" = --one-repo-only ]; then
    ONE_REPO_ONLY=true
  fi
done

git clone https://github.com/alisw/ali-bot

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

report_state started

while true; do
  if [ -n "$ONE_REPO_ONLY" ]; then
    # All the environment variables we need are already set by aurora, so just
    # run the build.
    (. build-loop.sh)
  else
    cur_container=${CONTAINER_IMAGE#*/}
    cur_container=${cur_container%-builder:*}
    # If there is no repo we can build in this container, wait; maybe something
    # will turn up.
    if ! [ -d "ali-bot/ci/repo-config/$cur_container" ]; then
      sleep 600
      continue
    fi

    # Loop through repositories we can build (i.e. that need the docker
    # container we are in).
    for env_file in "ali-bot/ci/repo-config/$cur_container"/*.env; do
      # Run iterations in a subshell so environment variables are not kept
      # across potentially different repos. This is an issue as env files are
      # allowed to define arbitrary variables that other files (or the defaults
      # file) might not override.
      (
        # Set up environment
        . "ali-bot/ci/repo-config/defaults.env" || exit
        . "$env_file"                           || exit   # in case there are no env files

        # Make a directory for this repo's dependencies so they don't conflict
        # with other repos'
        repo=${env_file#repo-config/$cur_container/}
        repo=${repo%.env}
        mkdir -p "$repo"
        cd "$repo" || exit 10

        # Get dependency development packages
        echo "$DEVEL_PKGS" | while read -r gh_url branch checkout_name; do
          git clone "https://github.com/$gh_url" ${branch:+--branch "$branch"} ${checkout_name:+"$checkout_name"}
        done

        # Run the build
        . build-loop.sh
      )
    done
  fi

  # Get updates to ali-bot
  reset_git_repository ali-bot
done
