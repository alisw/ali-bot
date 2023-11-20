#!/bin/bash -ex

# Turn OFF pipefail; things like `false | true` will not break execution in
# spite of `set -e`
set +o pipefail

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
elif [[ -d /cvmfs/alice-test.cern.ch ]]; then
  CONF=aliPublish-test.conf
  CMD=sync-cvmfs
elif [[ -d /cvmfs/alice-nightlies.cern.ch ]]; then
  CONF=aliPublish-nightlies.conf
  CMD=sync-cvmfs
  PUB_CCDB=1
  PUB_DATA=1
  PUB_CERT=1
  export PATH=$HOME/opt/bin:$PATH
elif [[ -d /cvmfs/alice.cern.ch ]]; then
  CMD=sync-cvmfs
  PUB_CCDB=1
  PUB_DATA=1
  PUB_CERT=1
  export PATH=$HOME/opt/bin:$PATH
elif [[ -d /cvmfs ]]; then
  CMD=sync-cvmfs
else
  false
fi
: "${CMD:?}" "${DEST=ali-bot}"
[[ ! -e $DEST/.git ]] && git clone https://github.com/alisw/ali-bot "$DEST"
logdir=${NOMAD_TASK_DIR-$PWD}/log
mkdir -p "$logdir"
find "$logdir" -type f -mtime +3 -delete || true
LOG=$logdir/log-$(date -u +%Y%m%d-%H%M%S%z)

# Export NO_UPDATE to prevent automatic updates
if [[ ! $NO_UPDATE ]]; then
  pushd $DEST
    git clean -fd
    git clean -fxd
    git remote update -p
    git fetch
    git fetch --tags
    git reset --hard origin/$(git rev-parse --abbrev-ref HEAD)
  popd
fi

venv=${NOMAD_TASK_DIR-$PWD}/venv
rm -rf "$venv"
python3 -m venv "$venv"
. "$venv/bin/activate"
pip install -U "$DEST"

ln -nfs "$(basename "$LOG.error")" "$logdir/latest"
cachedir=${NOMAD_TASK_DIR-$PWD}/cache
mkdir -p "$cachedir"
pushd $DEST/publish

  echo "Running version $(git rev-parse HEAD)"
  ERR=

  # Packages publisher
  ./${ALIPUBLISH:-aliPublishS3} --debug            \
               ${DRYRUN:+--dry-run}                \
               ${NO_NOTIF:+--no-notification}      \
               ${CONF:+--config "$CONF"}           \
               ${OVERRIDE:+--override "$OVERRIDE"} \
               --cache-deps-dir "$cachedir"        \
               --pidfile /tmp/aliPublish.pid       \
               $CMD                                \
               2>&1 | tee -a $LOG.error
  [[ ${PIPESTATUS[0]} == 0 ]] || ERR="$ERR packages"

  # CCDB caching
  if [[ $PUB_CCDB == 1 ]]; then
    ./cache-ccdb.py cache-ccdb-objects.txt 2>&1 | tee -a "$LOG.error"
    [[ ${PIPESTATUS[0]} == 0 ]] || ERR="$ERR ccdb"
  fi

  # Data publisher (e.g. OADB)
  if [[ $PUB_DATA == 1 ]]; then
    ./publish-data.sh 2>&1 | tee -a $LOG.error
    [[ ${PIPESTATUS[0]} == 0 ]] || ERR="$ERR data"
  fi

  # Certificates publisher
  if [[ $PUB_CERT == 1 ]]; then
    ./publish-cert.sh 2>&1 | tee -a $LOG.error
    [[ ${PIPESTATUS[0]} == 0 ]] || ERR="$ERR certificates"
  fi

popd

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
        -r 'ALICE Builder <alibot@cern.ch>'                     \
        -a "$TD/log.txt"                                        \
        alibuild@cern.ch                                        \
  && echo "$NOW $CONSECUTIVE_ERRORS" > $NOTIFICATION_STATE_FILE \
  || echo "0    $CONSECUTIVE_ERRORS" > $NOTIFICATION_STATE_FILE
}

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
  cp -v "$DEST/publish/get-and-run.sh" .
fi
