#!/bin/bash -e

# build-rpm-sync.sh -- by Dario Berzano
#
# Invoke the standard build-any-ib script, but check whether a RPM corresponding
# to the built package was eventually generated. The build and publishing
# process remain synchronous, but having this script waiting until the RPM is
# there (with a timeout) allows to synchronize the workflow. When run as a
# Jenkins job, a final green state means that the RPM was finally produced.

function pp() {
  printf "[BuildRPM] $1\n" >&2
}

function check_rpms() {
  local TMP_LIST="tmp_list.txt"
  local RSYNC_START_TIME=$(date +%s)
  if [[ $1 == --pkg ]]; then
    pp "Checking for new RPMs for $PACKAGE_NAME"
    local RSYNC_FILTER='alisw-'${PACKAGE_NAME}'\+.*\.rpm'
  elif [[ $1 == --repodata ]]; then
    pp "Checking if createrepo is in progress"
    local RSYNC_FILTER='\.repodata'
  else
    pp "FATAL: invalid option to check_rpms"
    exit 3
  fi
  while ! rsync --list-only -r ${REMOTE_STORE_LIST}/RPMS > "$TMP_LIST"; do
    if [[ $(( $(date +%s) - RSYNC_START_TIME )) -gt 600 ]]; then
      pp "FATAL: timed out while attempting to fetch list of RPMs"
      exit 2
    fi
    pp "Error calling rsync: retrying in 5 seconds"
    sleep 5
  done
  grep -oE "$RSYNC_FILTER" -- "$TMP_LIST" | sort -u
  rm -f "$TMP_LIST"
}

# What remote store to use. Variable is consistent with build-any-ib.sh
REMOTE_STORE_LIST="${REMOTE_STORE:-rsync://repo.marathon.mesos/store/}"

# Configure defaults for build-any-ib.sh (other vars come from Jenkins)
export PUBLISH_BUILDS="true"
export USE_REMOTE_STORE="true"

: ${WAIT_RPMS_TIMEOUT:=7200}    # Timeout [s] waiting for RPMs
: ${WAIT_RPMS_RETRY_SLEEP:=60}  # How much time [s] to sleep between RPM checks

check_rpms --pkg > list_rpms_before.txt
pp "Found $(cat list_rpms_before.txt | wc -l | xargs echo) RPMs for $PACKAGE_NAME"

pp "Launching aliBuild"
PROG_DIR=$(cd "$(dirname "$0")"; pwd)
"$PROG_DIR"/build-any-ib.sh

START_TIME=$(date +%s)

# Wait for new RPMs to appear
while [[ 1 ]]; do
  check_rpms --pkg > list_rpms_after.txt
  ERR=0
  diff list_rpms_before.txt list_rpms_after.txt | grep '> ' || ERR=$?
  if [[ $ERR == 0 ]]; then
    pp "New RPM found for $PACKAGE_NAME"
    break
  fi
  NOW_TIME=$(date +%s)
  ELAPSED_TIME=$(( NOW_TIME - START_TIME ))
  LEFT_TIME=$(( WAIT_RPMS_TIMEOUT - ELAPSED_TIME ))
  if [[ $ELAPSED_TIME -gt $WAIT_RPMS_TIMEOUT ]]; then
    pp "FATAL: timed out while waiting for new RPMs, sorry"
    exit 1
  fi
  pp "No new RPM found yet for $PACKAGE_NAME: checking in $WAIT_RPMS_RETRY_SLEEP s"
  pp "Will keep checking for the next $LEFT_TIME s"
  sleep $WAIT_RPMS_RETRY_SLEEP
done

# Wait for createrepo to settle
while [[ 1 ]]; do
  HAS_REPODATA=$(check_rpms --repodata)
  if [[ $HAS_REPODATA ]]; then
    NOW_TIME=$(date +%s)
    ELAPSED_TIME=$(( NOW_TIME - START_TIME ))
    LEFT_TIME=$(( WAIT_RPMS_TIMEOUT - ELAPSED_TIME ))
    if [[ $ELAPSED_TIME -gt $WAIT_RPMS_TIMEOUT ]]; then
      pp "FATAL: timed out while waiting for RPM repository update, sorry"
      exit 4
    fi
    pp "RPM repository update is in progress: checking in $WAIT_RPMS_RETRY_SLEEP s"
    pp "Will keep checking for the next $LEFT_TIME s"
    sleep $WAIT_RPMS_RETRY_SLEEP
  else
    pp "RPM repository update is complete: success"
    break
  fi
done

exit 0
