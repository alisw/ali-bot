#!/bin/bash

# Check if variables are provided
if [[ ! $SYNC_USER || ! $SYNC_PASS ]]; then
  echo "Variables SYNC_USER and SYNC_PASS are required, aborting" >&2
  exit 1
fi

for CONF in aliPublish*-rpms.conf; do

  echo === $(LANG=C date) :: running for configuration $CONF === >&2
  ./aliPublish --config "$CONF" --debug --cache-deps-dir /tmp/pubdepscache sync-rpms >&2
  printf "\n\n\n\n" >&2

  echo === $(LANG=C date) :: syncing to CERN IT EOS repo === >&2
  timeout -s 9 1800 \
    rsync --progress \
          --size-only \
          --delete \
          --rsh="sshpass -p '$SYNC_PASS' ssh -oUserKnownHostsFile=/dev/null -oStrictHostKeyChecking=no -l $SYNC_USER" \
          -rv \
          /repo/RPMS/el7.x86_64/ \
          lxplus.cern.ch:/eos/user/a/alibot/www/RPMS/el7.x86_64/ >&2
  printf "\n\n\n\n" >&2

done
