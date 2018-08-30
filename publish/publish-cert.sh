#!/bin/bash -ex

# Sync certificates from the CERN IT-managed directory to CVMFS. By default it
# runs from 5am every morning, determined by the system's timezone.
#
# Note that:
#
#  * the destination directory is completely replaced by source certificates,
#  * it is possible to define exceptions (i.e. manually-added certificates),
#  * certificate revocation lists are *not* copied: they are erased on the
#    destination
#
# Use FORCE=1 to force this script to sync certs in any case.
# Use DRYRUN=1 to see what would be done without actually executing it.
#
# Usage example:
#   env DRYRUN=1 FORCE=1 ./publish-cert.sh

cd /
REPO=$(cvmfs_server info 2> /dev/null | grep 'Repository name' | cut -d: -f2 | xargs echo)
[[ $REPO == *.cern.ch ]] || REPO=alice.cern.ch
[[ $DRYRUN ]] && cvmfs_server() { echo "[DRYRUN] cvmfs_server $*"; } || true

dieabort() {
  cd /
  cvmfs_server abort -f "$REPO" || true
  exit 1
}

cvmfs_lazy_transaction() {
  [[ $CVMFS_IN_TRANSACTION ]] && return 0
  for I in {0..7}; do
    ERR=0
    cvmfs_server transaction "$REPO" && CVMFS_IN_TRANSACTION=1 || ERR=$?
    [[ $ERR == 0 ]] && break || sleep 7
  done
  [[ $ERR != 0 ]] && echo "Cannot open transaction"
  return $ERR
}

cvmfs_lazy_publish() {
  [[ $CVMFS_IN_TRANSACTION ]] && { cvmfs_server publish "$REPO" || return $?; }
  return 0
}

CVMFS_IN_TRANSACTION=
CERT_SRC="/etc/grid-security/certificates"
CERT_ADDITIONAL_SRC="/cvmfs/$REPO/etc/grid-security/.additional_certificates"
CERT_DST="/cvmfs/$REPO/etc/grid-security/certificates"
[[ $DRYRUN ]] || { cvmfs_server &> /dev/null || [[ $? != 127 ]]; }

# Do we need to run today? Check timestamp of destination. If it's today's, or greater's, then exit.
# This method ensures that we keep trying until we get today's run done
DEST_TIMESTAMP=20000101  # e.g. YYYYMMDD
TODAY_TIMESTAMP=$(date +%Y%m%d)  # we are assuming the timezone on the running machine is OK
TODAY_HOUR=$(date +%_H)
[[ -d $CERT_DST ]] && DEST_TIMESTAMP=$(date -d @$(stat -c %Y "$CERT_DST") +%Y%m%d)
if [[ $FORCE ]]; then
  echo "Forcing syncing as requested"
elif [[ $TODAY_HOUR -lt 5 ]]; then
  echo "Not syncing before 5am, exiting"
  exit 0
elif [[ $DEST_TIMESTAMP -ge $TODAY_TIMESTAMP ]]; then
  echo "Certificates have already been updated to $CERT_DST today, exiting"
  exit 0
fi

# Do the sync
cvmfs_lazy_transaction || dieabort
[[ $DRYRUN ]] || mkdir -p "$CERT_DST"
echo "Syncing standard certificates from $CERT_SRC"
rsync -av ${DRYRUN:+-n} --delete --delete-excluded --exclude '*.r0' \
      "${CERT_SRC}/" "${CERT_DST}/"  # this step deletes CRLs too
if [[ -d $CERT_ADDITIONAL_SRC ]]; then
  echo "Syncing additional certificates from $CERT_ADDITIONAL_SRC"
  rsync -av ${DRYRUN:+-n} --exclude '*.r0' --exclude '*.txt' \
        "${CERT_ADDITIONAL_SRC}/" "${CERT_DST}/"
fi
[[ $DRYRUN ]] || touch "$CERT_DST"
cvmfs_lazy_publish || dieabort

# All OK
echo "Certificates have been updated from $CERT_SRC to $CERT_DST (with no CRLs)"
