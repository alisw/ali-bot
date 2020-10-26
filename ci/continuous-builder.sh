#!/bin/bash -x
# A simple script which keeps building using the latest aliBuild, alidist and
# AliRoot / AliPhysics. Notice this will do an incremental build, not a full
# build, so it really to catch errors earlier.

# timeout vs. gtimeout (macOS with Homebrew)
TIMEOUT_EXEC=timeout
type $TIMEOUT_EXEC > /dev/null 2>&1 || TIMEOUT_EXEC=gtimeout
function short_timeout () { $TIMEOUT_EXEC -s9 "$TIMEOUT" "$@"; }
function long_timeout () { $TIMEOUT_EXEC -s9 "$LONG_TIMEOUT" "$@"; }

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

# Clear list of idling repositories
: > nothing-to-do

if [ -n "$ONE_REPO_ONLY" ]; then
  # All the environment variables we need are already set by aurora, so just
  # run the build.
  (. build-loop.sh)
else
  # This turns a container image (e.g. alisw/slc8-gpu-builder:latest) into a
  # short, simple name like slc8-gpu that we use for the .env directories.
  cur_container=${CONTAINER_IMAGE#*/}
  cur_container=${cur_container%-builder:*}

  # Loop through repositories we can build (i.e. that need the docker
  # container we are in).
  for env_file in "ali-bot/ci/repo-config/$MESOS_ROLE/$cur_container"/*.env; do
    if [[ "$(basename "$env_file")" =~ (DEFAULTS|\*)\.env ]]; then
      # Skip this iteration if we've got the defaults file or if the glob
      # didn't match anything.
      continue
    fi

    # Run iterations in a subshell so environment variables are not kept
    # across potentially different repos. This is an issue as env files are
    # allowed to define arbitrary variables that other files (or the defaults
    # file) might not override.
    (
      # Set up environment
      # Load more specific defaults later so they override more general ones.
      . ali-bot/ci/repo-config/DEFAULTS.env                              || true
      . "ali-bot/ci/repo-config/$MESOS_ROLE/DEFAULTS.env"                || true
      . "ali-bot/ci/repo-config/$MESOS_ROLE/$cur_container/DEFAULTS.env" || true
      . "$env_file"

      # Make a directory for this repo's dependencies so they don't conflict
      # with other repos'
      repo=$(basename "${env_file%.env}")
      mkdir -p "$repo"
      cd "$repo" || exit 10

      # Get dependency development packages
      echo "$DEVEL_PKGS" | while read -r gh_url branch checkout_name; do
        : "${checkout_name:=$(basename "$gh_url")}"
        if [ -d "$checkout_name" ]; then
          reset_git_repository "$checkout_name"
        else
          git clone "https://github.com/$gh_url" ${branch:+--branch "$branch"} "$checkout_name"
        fi
      done

      pip2 install --upgrade --upgrade-strategy only-if-needed "git+https://github.com/$INSTALL_ALIBOT"
      pip2 install --upgrade --upgrade-strategy only-if-needed "git+https://github.com/$INSTALL_ALIBUILD"

      # Run the build
      . build-loop.sh
    )
  done
fi

get_config

# Get updates to ali-bot
reset_git_repository ali-bot

if [ -z "$ONE_REPO_ONLY" ] && [ -r nothing-to-do ]; then
  num_repos=0
  for envf in "ali-bot/ci/repo-config/$MESOS_ROLE/$cur_container"/*.env; do
    case "$envf" in
      */DEFAULTS.env) ;;
      *) num_repos=$((num_repos + 1));;
    esac
  done
  if [ "$(wc -l < nothing-to-do)" -eq "$num_repos" ]; then
    sleep 1200
  fi
fi

exec continuous-builder.sh
