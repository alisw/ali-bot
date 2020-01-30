#!/bin/bash -ex

# Turn OFF pipefail; things like `false | true` will not break execution in
# spite of `set -e`
set +o pipefail

# State of the last notification. If existing, it contains one line with:
# last_notification_timestamp consecutive_errors
NOTIFICATION_STATE_FILE=/tmp/publisher_notification_snoozer

export LANG=C
cd "$(dirname "$0")"
if [[ -x /home/monalisa/bin/alien ]]; then
  export PATH="/home/monalisa/bin:$PATH"
  CMD=sync-alien
  OVERRIDE='{"notification_email":{}}'
elif [[ -d /lustre/atlas/proj-shared/csc108 && -d /lustre/atlas/proj-shared/csc108 ]]; then
  # Titan needs some magic.
  source /usr/share/Modules/init/bash
  eval $(modulecmd bash load git/2.2.0)
  git --help > /dev/null 2>&1
  FAKECVMFS=/lustre/atlas/proj-shared/csc108/psvirin/publisher/.fakecvmfs
  mkdir -p $FAKECVMFS
  ln -nfs $(which true) $FAKECVMFS/cvmfs_server
  export PATH="$FAKECVMFS:$PATH"
  [[ ! -e alibuild/.git ]] && git clone https://github.com/alisw/alibuild
  [[ ! -e requests/.git ]] && git clone https://github.com/kennethreitz/requests -b v2.6.0
  export PYTHONPATH="$PWD/alibuild:$PWD/requests:$PYTHONPATH"
  CONF=aliPublish-titan.conf
  CMD=sync-cvmfs
elif [[ -d /cvmfs/alice-test.cern.ch ]]; then
  CONF=aliPublish-test.conf
  CMD=sync-cvmfs
elif [[ -d /cvmfs/alice-nightlies.cern.ch ]]; then
  CONF=aliPublish-nightlies.conf
  CMD=sync-cvmfs
  PUB_DATA=1
  PUB_CERT=1
  export PATH=$HOME/opt/bin:$PATH
elif [[ -d /cvmfs/alice.cern.ch ]]; then
  CMD=sync-cvmfs
  PUB_DATA=1
  PUB_CERT=1
  export PATH=$HOME/opt/bin:$PATH
elif [[ -d /cvmfs ]]; then
  CMD=sync-cvmfs
else
  false
fi
CMD="$CMD"
DEST=ali-bot
DRYRUN=${DRYRUN:-}
[[ ! -e $DEST/.git ]] && git clone https://github.com/alisw/ali-bot $DEST
mkdir -p log
find log/ -type f -mtime +3 -delete || true
LOG="$PWD/log/log-$(date -u +%Y%m%d-%H%M%S%z)"

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

ln -nfs $(basename $LOG.error) $PWD/log/latest
CACHE=$PWD/cache
mkdir -p $CACHE
pushd $DEST/publish

  echo "Running version $(git rev-parse HEAD)"
  ERR=

  # Packages publisher
  ./aliPublish --debug                             \
               ${DRYRUN:+--dry-run}                \
               ${NO_NOTIF:+--no-notification}      \
               ${CONF:+--config "$CONF"}           \
               ${OVERRIDE:+--override "$OVERRIDE"} \
               --cache-deps-dir $CACHE             \
               --pidfile /tmp/aliPublish.pid       \
               $CMD                                \
               2>&1 | tee -a $LOG.error
  [[ ${PIPESTATUS[0]} == 0 ]] || ERR="$ERR packages"

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
  ln -nfs $(basename $LOG) log/latest
fi

# Get new version of this script
[[ -x $DEST/publish/get-and-run.sh ]] && cp -v $DEST/publish/get-and-run.sh .
