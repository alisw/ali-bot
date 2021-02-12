#!/bin/bash

# Check if variables are provided.
[ X${DEBUG:-1} = X1 ] && set -x

python -m cProfile -s cumtime ./aliPublish --config "aliPublish-rpms.conf" --debug --cache-deps-dir /tmp/pubdepscache sync-rpms >&2
timeout 300 rclone sync --config /secrets/alibuild_rclone_config --transfers=10 --verbose local:/repo/RPMS/el7.x86_64/ rpms3:alibuild-repo/RPMS/el7.x86_64/ || true
