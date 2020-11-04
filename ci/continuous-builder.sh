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

if ! [ -d ali-bot ]; then
  git clone https://github.com/alisw/ali-bot
fi

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

# This turns a container image (e.g. alisw/slc8-gpu-builder:latest) into a
# short, simple name like slc8-gpu that we use for the .env directories.
CUR_CONTAINER=${CONTAINER_IMAGE#*/}
export CUR_CONTAINER=${CUR_CONTAINER%-builder:*}

# Generate example of force-hashes file. This is used to override what to check for testing
if ! [ -e force-hashes ]; then
  cat > force-hashes <<EOF
# Override what to build using this file.
# Lines are of the form:
# BUILD_TYPE (PR_NUMBER|BRANCH_NAME) PR_HASH ENV_NAME
# Where:
# - BUILD_TYPE: one of: not_tested, not_successful, tested, reviewed
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
  echo "$HASHES" | cat -n | while read -r BUILD_SEQ BUILD_TYPE PR_NUMBER PR_HASH env_name; do
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

    # Sometimes pip gets stuck when cloning the ali-bot or alibuild repos. In
    # that case: time out, skip and try again later.
    short_timeout pip2 install --upgrade --upgrade-strategy only-if-needed "git+https://github.com/$INSTALL_ALIBOT"   || exit
    short_timeout pip2 install --upgrade --upgrade-strategy only-if-needed "git+https://github.com/$INSTALL_ALIBUILD" || exit

    # Run the build
    . build-loop.sh
  done
else
  # If we're idling, wait a while to conserve GitHub API requests.
  sleep "$(get_config_value timeout "${TIMEOUT:-600}")"
fi

# Get updates to ali-bot
reset_git_repository ali-bot

exec continuous-builder.sh
