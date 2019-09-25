#!/bin/bash

# Check if variables are provided.
[ X${SYNC_USER:+1} = X ] && { echo "Variables SYNC_USER missing, aborting"; exit 1; } 
[ X${SYNC_PASS:+1} = X ] && { echo "Variables SYNC_PASS missing, aborting"; exit 1; } 

for CONF in aliPublish*-rpms.conf; do

  echo === $(LANG=C date) :: running for configuration $CONF === >&2
  ./aliPublish --config "$CONF" --debug --cache-deps-dir /tmp/pubdepscache sync-rpms >&2
  printf "\n\n\n\n" >&2

  echo === $(LANG=C date) :: syncing to CERN IT EOS repo === >&2
  timeout -s 9 1800                                                                                                   \
    rsync --progress                                                                                                  \
          --update                                                                                                    \
          --delete                                                                                                    \
          --chown $SYNC_USER:z2                                                                                       \
          --rsh="sshpass -p '$SYNC_PASS' ssh -oUserKnownHostsFile=/dev/null -oStrictHostKeyChecking=no -l $SYNC_USER" \
          -rv                                                                                                         \
          /repo/*RPMS --exclude '**/DAQ/' --exclude '**/createrepo_cachedir/' --exclude '**/el5.x86_64/'              \
          lxplus.cern.ch:/eos/user/a/alibot/www/ >&2
  printf "\n\n\n\n" >&2

done
