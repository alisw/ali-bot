#!/bin/bash
set +e
. /etc/profile.d/enable-alice.sh
# enable-alice.sh doesn't work with set -e, so only enable it now.
set -exo pipefail

publish_s3 () {
  ./aliPublishS3 --config "$1" --debug sync-rpms
}

publish_both () {
  if ./aliPublish --config "$1" --debug sync-rpms; then
    timeout 300 rclone sync --config /secrets/alibuild_rclone_config --transfers=10 --verbose \
            "local:/repo/RPMS/$arch/" "rpms3:alibuild-repo/RPMS/$arch/" || true
  else
    echo "ERROR: aliPublish ($1) failed with $?; not syncing to S3" >&2
    return 1
  fi
}

# The architecture should correspond to the architecture built using the
# requested config files -- only these architectures' repos will be synced.
arch=$1
shift   # $@ now contains only the config files to read.

# This contains MATTERMOST_O2_RELEASE_INTEGRATION_URL, which we need.
. /secrets/ci_secrets
# aliPublish needs these S3 secrets.
# rclone gets them from /secrets/alibuild_rclone_config.
. /secrets/aws_bot_secrets
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

# This script is in the same directory as aliPublish{,S3} in the ali-bot repo.
cd "$(dirname "$0")"

while true; do
  # Reset the specified git repository to its original, remote state.
  branch=$(git rev-parse --abbrev-ref HEAD)
  git fetch -f origin "+$branch:refs/remotes/origin/$branch"
  git reset --hard "origin/$branch"
  git clean -fxd

  # Look for any canary files from Jenkins builds that finished since we last ran.
  # We'll remove these once we're done, so that the respective Jenkins jobs can
  # look for the RPMs they expect.
  # We build this file list now as aliPublish can take some time, and other builds
  # (that we don't want to notify yet) might finish in the meantime. We run
  # aliPublish only for one specific architecture, so we have
  # architecture-specific canary files under rpmstatus/$arch/.
  s3cmd ls "s3://alibuild-repo/rpmstatus/$arch/" | cut -b 32- > canaries.txt

  case "$arch" in
    el8.*) publish=publish_s3 ;;
    *) publish=publish_both ;;
  esac

  pip3 install -Ur ../requirements.txt
  for conf in "$@"; do
    # Save current list of RPMs so we can see which ones are new later.
    # Sort it so comm is happy.
    s3cmd ls "s3://alibuild-repo/RPMS/$arch/" | sed 's|.*/||' | sort > before.pkgs

    "$publish" "$conf"

    # Compare the file list to the dir now, to see which RPMs were published.
    s3cmd ls "s3://alibuild-repo/RPMS/$arch/" | sed 's|.*/||' | sort |
      comm -13 before.pkgs - > new.pkgs
    # Post in the Release Integration channel if we have new RPMs.
    if [ -s new.pkgs ]; then
      curl -ifsSX POST -H 'Content-Type: application/json' \
           -d "{\"text\": \"# New RPMs published for \`$arch\`

$(sed 's/^/- `/; s/$/`/' new.pkgs)\"}" \
           "$MATTERMOST_O2_RELEASE_INTEGRATION_URL" || true
    fi
    rm -f before.pkgs new.pkgs
  done

  # Now that we've uploaded all the new RPMs, we can delete the canary files to
  # tell Jenkins jobs that we're done.
  xargs -rtd '\n' -a canaries.txt s3cmd rm

  sleep 120
done
