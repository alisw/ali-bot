#!/bin/bash -ex

# The architecture should correspond to the architecture built using the
# requested config files -- only these architectures' repos will be synced.
arch=$1
shift
# $@ now contains only the config files to read.

. /etc/profile.d/enable-alice.sh
# aliPublish needs these S3 secrets.
# rclone gets them from /secrets/alibuild_rclone_config.
. /secrets/aws_bot_secrets
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

cd "$(dirname "$0")"
while true; do
  # Update the ali-bot repository clone to get updated configs.
  # Remote changes override local ones.
  git pull --rebase --strategy=recursive --strategy-option=theirs

  for conf in "$@"; do
    ./aliPublish --config "$conf" --debug sync-rpms >&2
  done

  timeout 300 rclone sync --config /secrets/alibuild_rclone_config --transfers=10 --verbose \
          "local:/repo/RPMS/$arch.x86_64/" "rpms3:alibuild-repo/RPMS/$arch.x86_64/" || true
  timeout 300 rclone sync --config /secrets/alibuild_rclone_config --transfers=10 --verbose \
          "local:/repo/UpdRPMS/$arch.x86_64/" "rpms3:alibuild-repo/UpdRPMS/$arch.x86_64/" || true

  sleep 60
done
