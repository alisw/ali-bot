#!/bin/bash -x
# A simple script which keeps building using the latest aliBuild, alidist and
# AliRoot / AliPhysics. Notice this will do an incremental build, not a full
# build, so it really to catch errors earlier.

# A few common environment variables when reporting status to analytics.
# In analytics we use screenviews to indicate different states of the
# processing and events to indicate all the things we would consider as
# fatal in a non deamon process but that here simly make us go to the
# next step.
echo ALIBUILD_O2_FORCE_GPU: $ALIBUILD_O2_FORCE_GPU
echo AMDAPPSDKROOT: $AMDAPPSDKROOT
echo CMAKE_PREFIX_PATH: $CMAKE_PREFIX_PATH
export ALIBOT_ANALYTICS_ID=$ALIBOT_ANALYTICS_ID
export ALIBOT_ANALYTICS_USER_UUID=`hostname -s`-$WORKER_INDEX${CI_NAME:+-$CI_NAME}
# Hardcode for now
export ALIBOT_ANALYTICS_ARCHITECTURE=slc7_x86-64
export ALIBOT_ANALYTICS_APP_NAME="continuous-builder.sh"

# Mesos DNSes
: ${MESOS_DNS:=alimesos01.cern.ch,alimesos02.cern.ch,alimesos03.cern.ch}
export MESOS_DNS

TIME_STARTED=$(date -u +%s)
CI_HASH=$(cd "$(dirname "$0")" && git rev-parse HEAD)

# timeout vs. gtimeout (macOS with Homebrew)
TIMEOUT_EXEC=timeout
type $TIMEOUT_EXEC > /dev/null 2>&1 || TIMEOUT_EXEC=gtimeout
function short_timeout () { $TIMEOUT_EXEC -s9 "$(get_config_value timeout "$TIMEOUT")" "$@"; }
function long_timeout () { $TIMEOUT_EXEC -s9 "$(get_config_value long-timeout "$LONG_TIMEOUT")" "$@"; }

. build-helpers.sh

MIRROR=${MIRROR:-/build/mirror}
PACKAGE=${PACKAGE:-AliPhysics}
LAST_PR=
PR_REPO_CHECKOUT=${PR_REPO_CHECKOUT:-$(basename "$PR_REPO")}

# If INFLUXDB_WRITE_URL starts with insecure_https://, then strip "insecure" and
# set the proper option to curl
INFLUX_INSECURE=
[[ $INFLUXDB_WRITE_URL == insecure_https:* ]] && { INFLUX_INSECURE=-k; INFLUXDB_WRITE_URL=${INFLUXDB_WRITE_URL:9}; }

# Last time `git gc` was run
LAST_GIT_GC=0

# This is the check name. If CHECK_NAME is in the environment, use it. Otherwise
# default to, e.g., build/AliRoot/release (build/<Package>/<Defaults>)
CHECK_NAME=${CHECK_NAME:=build/$PACKAGE${ALIBUILD_DEFAULTS:+/$ALIBUILD_DEFAULTS}}

# Worker index, zero-based. Set to 0 if unset (i.e. when not running on Aurora)
WORKER_INDEX=${WORKER_INDEX:-0}

pushd alidist
  ALIDIST_REF=`git rev-parse --verify HEAD`
popd
# Generate example of force-hashes file. This is used to override what to check for testing
if [[ ! -e force-hashes ]]; then
  cat > force-hashes <<EOF
# Example (this is a comment):
# pr_number@hash
# You can also use:
# branch_name@hash
EOF
fi


# Explicitly set UTF-8 support (Python needs it!)
export LANG="en_US.UTF-8"
export LANGUAGE="en_US.UTF-8"
export LC_CTYPE="en_US.UTF-8"
export LC_NUMERIC="en_US.UTF-8"
export LC_TIME="en_US.UTF-8"
export LC_COLLATE="en_US.UTF-8"
export LC_MONETARY="en_US.UTF-8"
export LC_MESSAGES="en_US.UTF-8"
export LC_PAPER="en_US.UTF-8"
export LC_NAME="en_US.UTF-8"
export LC_ADDRESS="en_US.UTF-8"
export LC_TELEPHONE="en_US.UTF-8"
export LC_MEASUREMENT="en_US.UTF-8"
export LC_IDENTIFICATION="en_US.UTF-8"
export LC_ALL="en_US.UTF-8"

report_state started

mkdir -p config

while true; do
  source `which build-helpers.sh || echo ci/build-helpers.sh`
  # We source the actual build loop, so that whenever we repeat it,
  # we can get a new version.
  source `which build-loop.sh || echo ci/build-loop.sh`
done
