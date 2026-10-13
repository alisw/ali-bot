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

# The architecture should correspond to the architecture built using the
# requested config files -- only these architectures' repos will be synced.
# Tolerate the old call signature for one deployment cycle. The publisher
# re-clones ali-bot master on every pass, so this script starts being used
# minutes after it is pushed, while the jobspec only changes when somebody
# re-registers the job -- in between, the still-running publish-rpm-el9.x86_64
# passes its architecture as $1, which would otherwise be read as a config
# filename and kill the loop through `set -e`. Drop it: the architecture list
# is derived from the configs below. Remove this once no job passes one.
case ${1:-} in
  ''|*.conf) ;;
  *) echo "NOTE: ignoring legacy architecture argument '$1'" >&2; shift ;;
esac

# The RPM architectures a config publishes, read out of its own
# `architectures:` map rather than passed in. aliPublish has always been
# multi-architecture -- aliPublish-s3-updatable-rpms.conf publishes slc8 as
# well as slc9 -- while this script took a single architecture positionally and
# looked only there, so the el8 half of that config was never diffed for
# Mattermost and its canaries were never cleared. Deriving the list means the
# bookkeeping cannot drift from what aliPublish actually writes, and that one
# positional argument was the only reason the Nomad job needed a Levant
# template: with it gone the job name is static and the jobspec is plain HCL.
conf_arches () {
  python3 -c '
import sys, yaml
conf = yaml.safe_load(open(sys.argv[1])) or {}
for name, maps in (conf.get("architectures") or {}).items():
    print(maps.get("RPM", name) if isinstance(maps, dict) else name)
' "$1"
}

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
  # (that we don't want to notify yet) might finish in the meantime. Canary
  # files are per architecture under rpmstatus/$arch/, so collect them for every
  # architecture any of our configs publishes.
  arches=$(for conf in "$@"; do conf_arches "$conf"; done | sort -u)
  : > canaries.txt
  for arch in $arches; do
    s3cmd ls "s3://alibuild-repo/rpmstatus/$arch/" | cut -b 32- >> canaries.txt
  done

  for conf in "$@"; do
    path=$(s3_path "$conf")
    conf_archlist=$(conf_arches "$conf")

    # Save current list of RPMs so we can see which ones are new later.
    # Sort it so comm is happy. Every architecture up front, because the single
    # aliPublish run below handles all of this config's architectures at once.
    for arch in $conf_archlist; do
      s3cmd ls "s3://alibuild-repo/$path/$arch/" | sed 's|.*/||' | sort > "before.$arch.pkgs"
    done

    # Deliberately not fatal, unlike every other command here. This is a
    # service loop that retries in two minutes anyway, and one config failing
    # -- a transient S3 error, a package that will not build -- used to exit
    # non-zero through `set -e` and take the whole publisher down with it,
    # including the architectures that were working. Now it is logged and the
    # next config still gets its turn.
    ./aliPublishS3 --config "$conf" --debug sync-rpms ||
      echo "WARNING: aliPublish failed for $conf (exit $?); continuing" >&2

    for arch in $conf_archlist; do
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
