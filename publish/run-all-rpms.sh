#!/bin/bash

# Check if variables are provided.
[ X${DEBUG:-1} = X1 ] && set -x

for CONF in aliPublish*-rpms.conf; do
  echo === $(LANG=C date) :: running for configuration $CONF === >&2
  ./aliPublish --config "$CONF" --debug sync-rpms >&2
  timeout 300 rclone sync --config /secrets/alibuild_rclone_config --transfers=10 --verbose local:/repo/RPMS/el7.x86_64/ rpms3:alibuild-repo/RPMS/el7.x86_64/ || true
  timeout 300 rclone sync --config /secrets/alibuild_rclone_config --transfers=10 --verbose local:/repo/UpdRPMS/el7.x86_64/ rpms3:alibuild-repo/UpdRPMS/el7.x86_64/ || true
  printf "\n\n\n\n" >&2
done
