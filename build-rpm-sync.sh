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

function clexit() {
  if [[ -d "$SCRATCH" ]]; then
    rm -rf "$SCRATCH"
  fi
  exit $1
}

function check_rpms() {
  local TMP_LIST="$SCRATCH/tmp_list.txt"
  local RSYNC_START_TIME=$(date +%s)
  if [[ $1 == --pkg ]]; then
    pp "Checking for new RPMs for $PACKAGE_NAME"
  elif [[ $1 == --repodata ]]; then
    pp "Checking if createrepo is in progress"
  else
    pp "FATAL: invalid option to check_rpms"
    clexit 3
  fi
  rm -f "$TMP_LIST"
  for RPM_REPO in $REMOTE_RPM_REPOS; do
    while ! rsync --list-only -r ${REMOTE_STORE_LIST}/${RPM_REPO} >> "$TMP_LIST"; do
      if [[ $(( $(date +%s) - RSYNC_START_TIME )) -gt 600 ]]; then
        pp "FATAL: timed out while attempting to fetch list of RPMs"
        clexit 2
      fi
      pp "Error calling rsync: retrying in 5 seconds"
      sleep 5
    done
  done
  if [[ $1 == --pkg ]]; then
    if [[ ! $JENKINS_URL ]]; then
      grep "$PACKAGE_NAME" "$TMP_LIST" >&2 || true
    fi
    local RPM
    while read RPM; do
      if [[ $RPM =~ ^(.*)-(.*)-(.*)\.(.*)\.(.*)\.rpm$ ]]; then
        local RPM_NAME=${BASH_REMATCH[1]}
        if [[ $RPM_NAME == alisw-${PACKAGE_NAME}+* || $RPM_NAME == alisw-${PACKAGE_NAME} ]]; then
          echo $RPM
        fi
      fi
    done < <( (grep -v '/staging/' "$TMP_LIST" || true) | (grep -oE alisw-.*rpm || true) | sort -u )
  elif [[ $1 == --repodata ]]; then
    grep -oE '\.repodata$' -- "$TMP_LIST" | sort -u
  fi
  rm -f "$TMP_LIST"
}

# What remote store to use. Variable is consistent with build-any-ib.sh
REMOTE_STORE_LIST="${REMOTE_STORE:-rsync://repo.marathon.mesos/store/}"

# Is package name set?
if [[ ! $PACKAGE_NAME ]]; then
  pp "No PACKAGE_NAME set, cannot continue"
  clexit 6
fi

# Remote RPM repositories
REMOTE_RPM_REPOS=
COUNT_ATTEMPTS=0
while [[ ! $REMOTE_RPM_REPOS && $COUNT_ATTEMPTS -lt 10 ]]; do
  REMOTE_RPM_REPOS=$(rsync --list-only "$REMOTE_STORE_LIST" | grep RPMS | awk '{ print $NF }' 2> /dev/null | xargs echo)
  COUNT_ATTEMPTS=$((COUNT_ATTEMPTS + 1))
done
if [[ ! $REMOTE_RPM_REPOS ]]; then
  pp "Cannot get list of RPM repositories, aborting"
  clexit 5
fi
pp "We will check for new RPMs in repositories: $REMOTE_RPM_REPOS"

# Configure defaults for build-any-ib.sh (other vars come from Jenkins)
export PUBLISH_BUILDS="true"
export USE_REMOTE_STORE="true"

: ${WAIT_RPMS_TIMEOUT:=7200}    # Timeout [s] waiting for RPMs
: ${WAIT_RPMS_RETRY_SLEEP:=60}  # How much time [s] to sleep between RPM checks

SCRATCH=$(mktemp -d)
check_rpms --pkg > "$SCRATCH/list_rpms_before.txt"
pp "Found $(cat "$SCRATCH/list_rpms_before.txt" | wc -l | xargs echo) RPMs for $PACKAGE_NAME"

if [[ $JENKINS_URL ]]; then
  pp "Launching aliBuild"
  PROG_DIR=$(cd "$(dirname "$0")"; pwd)
  "$PROG_DIR"/build-any-ib.sh
else
  # Not running through Jenkins: test mode
  pp "Not running aliBuild because not on Jenkins"
  WAIT_RPMS_RETRY_SLEEP=10
fi

START_TIME=$(date +%s)

# Wait for new RPMs to appear
while [[ 1 ]]; do
  check_rpms --pkg > "$SCRATCH/list_rpms_after.txt"
  ERR=0
  diff "$SCRATCH/list_rpms_before.txt" "$SCRATCH/list_rpms_after.txt" | grep '> ' || ERR=$?
  if [[ $ERR == 0 ]]; then
    pp "New RPM found for $PACKAGE_NAME"
    break
  fi
  NOW_TIME=$(date +%s)
  ELAPSED_TIME=$(( NOW_TIME - START_TIME ))
  LEFT_TIME=$(( WAIT_RPMS_TIMEOUT - ELAPSED_TIME ))
  if [[ $ELAPSED_TIME -gt $WAIT_RPMS_TIMEOUT ]]; then
    pp "FATAL: timed out while waiting for new RPMs, sorry"
    clexit 1
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
      clexit 4
    fi
    pp "RPM repository update is in progress: checking in $WAIT_RPMS_RETRY_SLEEP s"
    pp "Will keep checking for the next $LEFT_TIME s"
    sleep $WAIT_RPMS_RETRY_SLEEP
  else
    pp "RPM repository update is complete: success"
    break
  fi
done

clexit 0
