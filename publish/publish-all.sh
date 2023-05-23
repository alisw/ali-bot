#!/bin/sh -e
# This is for the rsync repository
ALIPUBLISH=aliPublishS3 CONF=aliPublish.conf ~/publisher/get-and-run.sh
# This is for the S3 / el8 and ARM repository
ALIPUBLISH=aliPublishS3 CONF=aliPublish-s3.conf ~/publisher/get-and-run.sh
# This is for the noarch repository
ALIPUBLISH=aliPublishS3 CONF=aliPublish-noarch.conf ~/publisher/get-and-run.sh
