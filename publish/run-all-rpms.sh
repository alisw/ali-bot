#!/bin/bash
set +e
. /etc/profile.d/enable-alice.sh
# enable-alice.sh doesn't work with set -e, so only enable it now.
set -exo pipefail

s3_path () {
  if grep -qEix 'rpm_updatable:[[:space:]]+true' "$1"; then
    echo UpdRPMS
  else
    echo RPMS
  fi
}

get_secrets () {
  # This contains MATTERMOST_O2_RELEASE_INTEGRATION_URL, which we need.
  . /secrets/ci_secrets
  # aliPublish needs these S3 secrets.
  # rclone gets them from /secrets/alibuild_rclone_config.
  . /secrets/aws_bot_secrets
  export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
}

# One or more architectures, comma-separated: "el9.x86_64,el10.x86_64". This
# does NOT select what gets published -- aliPublish publishes every entry of
# each config's architectures: map. It says which repos to diff for the
# Mattermost post and whose rpmstatus/ canaries to clear, so it should list
# every architecture the given configs can produce.
archs=${1//,/ }
shift   # $@ now contains only the config files to read.

get_secrets
trap get_secrets USR1

# This script is in the same directory as aliPublishS3 in the ali-bot repo.
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
  # (that we don't want to notify yet) might finish in the meantime. Canary files
  # are per architecture, under rpmstatus/$arch/.
  : > canaries.txt
  for arch in $archs; do
    s3cmd ls "s3://alibuild-repo/rpmstatus/$arch/" | cut -b 32- >> canaries.txt
  done

  for conf in "$@"; do
    path=$(s3_path "$conf")

    # Save current list of RPMs so we can see which ones are new later.
    # Sort it so comm is happy. All architectures first: one aliPublish run
    # publishes all of them, so a "before" taken afterwards would miss them.
    for arch in $archs; do
      s3cmd ls "s3://alibuild-repo/$path/$arch/" | sed 's|.*/||' | sort > "before.$arch.pkgs"
    done

    ./aliPublishS3 --config "$conf" --debug sync-rpms

    for arch in $archs; do
      # Compare the file list to the dir now, to see which RPMs were published.
      s3cmd ls "s3://alibuild-repo/$path/$arch/" | sed 's|.*/||' | sort |
        comm -13 "before.$arch.pkgs" - > new.pkgs
      # Post in the Release Integration channel if we have new RPMs.
      if [ -s new.pkgs ]; then
        curl -ifsSX POST -H 'Content-Type: application/json' \
             -d "{\"text\": \"# New RPMs published for \`$arch\`

$(sed 's/^/- `/; s/$/`/' new.pkgs)\"}" \
             "$MATTERMOST_O2_RELEASE_INTEGRATION_URL" || true
      fi
      rm -f "before.$arch.pkgs" new.pkgs
    done
  done

  # Now that we've uploaded all the new RPMs, we can delete the canary files to
  # tell Jenkins jobs that we're done.
  xargs -rtd '\n' -a canaries.txt s3cmd rm

  sleep 120
done
