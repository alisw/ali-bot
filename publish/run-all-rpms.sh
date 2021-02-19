#!/bin/bash
set -exo pipefail

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

  # Look for any canary files from Jenkins builds that finished since we last
  # ran. We'll remove these once we're done, so that the respective Jenkins jobs
  # can look for the RPMs they expect.
  # We build this file list now as aliPublish can take some time, and other
  # builds (that we don't want to notify yet) might finish in the meantime.
  # We run aliPublish only for one specific architecture, so we have
  # architecture-specific canary files under rpmstatus/$arch/.
  s3cmd ls "s3://alibuild-repo/rpmstatus/$arch/" | cut -b 32- > canaries.txt

  for conf in "$@"; do
    ./aliPublish --config "$conf" --debug sync-rpms >&2
  done

  timeout 300 rclone sync --config /secrets/alibuild_rclone_config --transfers=10 --verbose \
          "local:/repo/RPMS/$arch.x86_64/" "rpms3:alibuild-repo/RPMS/$arch.x86_64/" || true
  timeout 300 rclone sync --config /secrets/alibuild_rclone_config --transfers=10 --verbose \
          "local:/repo/UpdRPMS/$arch.x86_64/" "rpms3:alibuild-repo/UpdRPMS/$arch.x86_64/" || true

  # Now that we've uploaded all the new RPMs, we can delete the canary files to
  # tell Jenkins jobs that we're done.
  xargs -rtd '\n' -a canaries.txt s3cmd rm

  sleep 60
done
