#!/bin/sh -e
# This is for the rsync repository
$HOME/publisher/get-and-run.sh
# This is for the S3 repository
AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID         \
AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY \
ALIPUBLISH=aliPublishS3 CONF=aliPublish-async.conf $HOME/publisher/get-and-run.sh
