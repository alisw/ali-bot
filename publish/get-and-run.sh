#!/bin/bash -ex

# Turn OFF pipefail; things like `false | true` will not break execution in
# spite of `set -e`
set +o pipefail

TOTAL_START=$(date +%s)
echo "TIMING: Script started at $(date)"

# State of the last notification. If existing, it contains one line with:
# last_notification_timestamp consecutive_errors
NOTIFICATION_STATE_FILE=/tmp/publisher_notification_snoozer

export LANG=C
cd "$(dirname "$0")"
if [ -n "$NOMAD_TASK_DIR" ] && [ -d "$NOMAD_TASK_DIR" ]; then
  # We're running under Nomad, variables should be set by the job.
  : "${CMD:?Please set CMD}" "${CONF:?Please set CONF}" "${NO_UPDATE=true}"
  # Under Nomad, we run publish/get-and-run.sh directly from the repo.
  DEST=$(dirname "$(dirname "$0")")
elif [[ -x /home/monalisa/bin/alien ]]; then
  export PATH="/home/monalisa/bin:$PATH"
  CMD=sync-alien
  OVERRIDE='{"notification_email":{}}'
elif [[ -d /cvmfs/alice.cern.ch ]]; then
  CMD=sync-cvmfs
  PUB_CCDB=1
  PUB_DATA=1
  PUB_CERT=1
  export PATH=$HOME/opt/bin:$PATH
elif [[ -d /cvmfs/sft-nightlies-test.cern.ch ]]; then
  CONF=/home/cvsft-nightlies-test/lcgBitsPublish-test.conf
  CMD=sync-cvmfs
elif [[ -d /cvmfs ]]; then
  CMD=sync-cvmfs
else
  false
fi
: "${CMD:?}" "${DEST=ali-bot}"
if [[ ! -e $DEST/.git ]]; then
  echo "TIMING: Starting git clone at $(date)"
  CLONE_START=$(date +%s)
  git clone https://github.com/alisw/ali-bot "$DEST"
  echo "TIMING: Git clone took $(($(date +%s) - CLONE_START))s"
fi
logdir=${NOMAD_TASK_DIR-$PWD}/log
mkdir -p "$logdir"
find "$logdir" -type f -mtime +3 -delete || true
LOG=$logdir/log-$(date -u +%Y%m%d-%H%M%S%z)

# Export NO_UPDATE to prevent automatic updates
if [[ ! $NO_UPDATE ]]; then
  echo "TIMING: Starting git update at $(date)"
  UPDATE_START=$(date +%s)
  pushd $DEST
    git clean -fd
    git clean -fxd
    git remote update -p
    git fetch
    git fetch --tags
    git reset --hard origin/$(git rev-parse --abbrev-ref HEAD)
  popd
  echo "TIMING: Git update took $(($(date +%s) - UPDATE_START))s"
fi

echo "TIMING: Starting venv setup at $(date)"
VENV_START=$(date +%s)
venv=${NOMAD_TASK_DIR-$PWD}/venv.$$
rm -rf "$venv"
python3 -m venv "$venv"
echo "TIMING: Venv creation took $(($(date +%s) - VENV_START))s"
. "$venv/bin/activate"
# If DEST is in the cwd, it might be confused for a PyPI package, so make it
# an absolute path.
echo "TIMING: Starting pip install at $(date)"
PIP_START=$(date +%s)
timeout 300 python3 -m pip install -U "$(realpath "$DEST")"
echo "TIMING: Pip install took $(($(date +%s) - PIP_START))s"

ln -nfs "$(basename "$LOG.error")" "$logdir/latest"
cachedir=${NOMAD_TASK_DIR-$PWD}/cache
mkdir -p "$cachedir"
pushd $DEST/publish

  echo "Running version $(pip list --format freeze | grep ali-bot)"
  ERR=

  # Packages publisher
  echo "TIMING: Starting package publisher at $(date)"
  PKG_START=$(date +%s)
  ./${ALIPUBLISH:-aliPublishS3} --debug            \
               ${DRYRUN:+--dry-run}                \
               ${NO_NOTIF:+--no-notification}      \
               ${CONF:+--config "$CONF"}           \
               ${OVERRIDE:+--override "$OVERRIDE"} \
               --cache-deps-dir "$cachedir"        \
               --pidfile /tmp/aliPublish.pid       \
                $CMD                               \
                2>&1 | tee -a $LOG.error
  [[ ${PIPESTATUS[0]} == 0 ]] || ERR="$ERR packages"
  echo "TIMING: Package publisher took $(($(date +%s) - PKG_START))s"

  # CCDB caching
  if [[ $PUB_CCDB == 1 ]]; then
    echo "TIMING: Starting CCDB caching at $(date)"
    CCDB_START=$(date +%s)
    ./cache-ccdb.py cache-ccdb-objects.txt 2>&1 | tee -a "$LOG.error"
    [[ ${PIPESTATUS[0]} == 0 ]] || ERR="$ERR ccdb"
    echo "TIMING: CCDB caching took $(($(date +%s) - CCDB_START))s"
  fi

  # Data publisher (e.g. OADB)
  if [[ $PUB_DATA == 1 ]]; then
    echo "TIMING: Starting data publisher at $(date)"
    DATA_START=$(date +%s)
    ./publish-data.sh 2>&1 | tee -a $LOG.error
    [[ ${PIPESTATUS[0]} == 0 ]] || ERR="$ERR data"
    echo "TIMING: Data publisher took $(($(date +%s) - DATA_START))s"
  fi

  # Certificates publisher
  if [[ $PUB_CERT == 1 ]]; then
    echo "TIMING: Starting certificate publisher at $(date)"
    CERT_START=$(date +%s)
    ./publish-cert.sh 2>&1 | tee -a $LOG.error
    [[ ${PIPESTATUS[0]} == 0 ]] || ERR="$ERR certificates"
    echo "TIMING: Certificate publisher took $(($(date +%s) - CERT_START))s"
  fi

popd

echo "TIMING: TOTAL EXECUTION TIME: $(($(date +%s) - TOTAL_START))s"

function notify_on_error() {
  # Policy:
  # * Only send email upon the 6th consecutive error
  # * Once sending is successful, stay silent for the next 6 hours
  # * Full success resets all counters
  local NOW=$(date --utc +%s)
  local LAST_STATE=$(cat $NOTIFICATION_STATE_FILE 2>/dev/null || echo "0 0")
  local LAST_NOTIFICATION_TIMESTAMP=$(echo $LAST_STATE | cut -d' ' -f1)
  local CONSECUTIVE_ERRORS=$(echo $LAST_STATE | cut -d' ' -f2)
  local ELAPSED=$(( NOW - LAST_NOTIFICATION_TIMESTAMP ))

  CONSECUTIVE_ERRORS=$(( CONSECUTIVE_ERRORS + 1 ))
  if [[ $CONSECUTIVE_ERRORS -lt 6 || $ELAPSED -lt 21600 ]]; then
    echo "Will not notify now: consecutive errors is $CONSECUTIVE_ERRORS, elapsed is $ELAPSED s"
    echo "$LAST_NOTIFICATION_TIMESTAMP $CONSECUTIVE_ERRORS" > $NOTIFICATION_STATE_FILE
    return 0
  fi

  local ATTACHMENT="$2"
  local TD=$(mktemp -d)
  local REPO=$(ls -1 /cvmfs|head -n1|cut -d. -f1)
  [[ $REPO ]] && REPO=" for $REPO" || true
  cp "$ATTACHMENT" "$TD/log.txt"
  echo "$1" | \
  mailx -s "[AliBuild${REPO}] An error occurred"                \
        -r 'ALICE Builder <ali-bot@cern.ch>'                    \
        -a "$TD/log.txt"                                        \
        alibuild@cern.ch                                        \
  && echo "$NOW $CONSECUTIVE_ERRORS" > $NOTIFICATION_STATE_FILE \
  || echo "0    $CONSECUTIVE_ERRORS" > $NOTIFICATION_STATE_FILE
}

echo "TIMING: Starting cleanup at $(date)"
CLEANUP_START=$(date +%s)
deactivate
rm -rf "$venv"
echo "TIMING: Cleanup took $(($(date +%s) - CLEANUP_START))s"
if [[ $ERR ]]; then
  ERR=$(echo $ERR | sed -e 's/ /, /g')
  echo "Something went wrong while publishing: $ERR"
  notify_on_error "Something went wrong while publishing: $ERR" "$LOG.error"
else
  echo "All OK"
  mv -v $LOG.error $LOG
  rm -f "$NOTIFICATION_STATE_FILE"
  ln -nfs "$(basename "$LOG")" "$logdir/latest"
fi

# Get new version of this script
if [ -z "$NO_UPDATE" ] && [ -x "$DEST/publish/get-and-run.sh" ]; then
  echo "TIMING: Copying updated script at $(date)"
  cp -v "$DEST/publish/get-and-run.sh" .
fi

echo "TIMING: Script finished at $(date)"
[ -z "$ERR" ]   # exit with failure if any errors occurred
