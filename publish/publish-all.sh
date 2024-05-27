#!/bin/sh -e
# This is for the S3 repository.
ALIPUBLISH=aliPublishS3 CONF=aliPublish.conf ~/publisher/get-and-run.sh
# This is for the noarch repository (needs special handling because it
# overlaps with a stanza in aliPublish.conf).
ALIPUBLISH=aliPublishS3 CONF=aliPublish-noarch.conf ~/publisher/get-and-run.sh
