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



# Set up common global environment
# Mesos DNSes
: "${MESOS_DNS:=alimesos01.cern.ch,alimesos02.cern.ch,alimesos03.cern.ch}"
export MESOS_DNS
# Explicitly set UTF-8 support (Python needs it!)
export {LANG{,UAGE},LC_{CTYPE,NUMERIC,TIME,COLLATE,MONETARY,PAPER,MESSAGES,NAME,ADDRESS,TELEPHONE,MEASUREMENT,IDENTIFICATION,ALL}}=en_US.UTF-8

report_state started
# GitLab credentials for private ALICE repositories
printf 'protocol=https\nhost=gitlab.cern.ch\nusername=%s\npassword=%s\n' "$GITLAB_USER" "$GITLAB_PASS" |
  git credential-store --file ~/.git-creds store
git config --global credential.helper 'store --file ~/.git-creds'


while true; do
  source `which build-helpers.sh || echo ci/build-helpers.sh`
  # We source the actual build loop, so that whenever we repeat it,
  # we can get a new version.
  source `which build-loop.sh || echo ci/build-loop.sh`
done
