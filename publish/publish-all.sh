#!/bin/sh -e
# This is for the rsync repository
$HOME/publisher/get-and-run.sh
# This is for the S3 / el8 repository
ALIPUBLISH=aliPublishS3 CONF=aliPublish-async.conf $HOME/publisher/get-and-run.sh
# This is for the noarch repository
ALIPUBLISH=aliPublishS3 CONF=aliPublish-noarch.conf $HOME/publisher/get-and-run.sh
