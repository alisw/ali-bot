#!/bin/bash -x

yum install -y rclone
pip install -U boto3

. /secrets/alibot_secrets
. /etc/profile.d/enable-alice.sh
export FLP_RPM_CI_TOKEN SYNC_PASS=$ALIBOT_PASSWORD SYNC_USER=alibot

cd "$(dirname "$0")" || exit 2
while true; do
  # Supply possible conf suffix, e.g. -cc8, as arg to this script.
  for conf in aliPublish*-rpms"$1".conf; do
    echo "=== $(LANG=C date) :: running for configuration $conf ===" >&2
    ./aliPublish --config "$conf" --debug sync-rpms >&2 || exit 1
    for rpmtype in RPMS UpdRPMS; do
      for arch in el7.x86_64 el8.x86_64; do
        timeout 300 rclone sync --config /secrets/alibuild_rclone_config --transfers=10 --verbose \
                "local:/repo/$rpmtype/$arch/" "rpms3:alibuild-repo/$rpmtype/$arch/"
      done
    done
    printf '\n\n\n\n' >&2
  done
  sleep 600
done
