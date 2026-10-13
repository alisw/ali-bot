#!/bin/sh -e
# One get-and-run.sh invocation per configuration. The CCDB cache, the data
# publisher and the certificate publisher are per host rather than per config,
# so only the first invocation runs them; the rest pass PUB_*=0.

# This is for the S3 repository.
ALIPUBLISH=aliPublishS3 CONF=aliPublish.conf ~/publisher/get-and-run.sh
# This is for the noarch repository (needs special handling because it
# overlaps with a stanza in aliPublish.conf).
ALIPUBLISH=aliPublishS3 CONF=aliPublish-noarch.conf \
  PUB_CCDB=0 PUB_DATA=0 PUB_CERT=0 ~/publisher/get-and-run.sh
# el10-x86_64 bring-up: separate from aliPublish.conf so the architecture can be
# published, inspected and stopped on its own until it is folded in. Needs no S3
# credentials -- sync-cvmfs reads the repository unsigned.
ALIPUBLISH=aliPublishS3 CONF=aliPublish-cvmfs-slc10.conf \
  PUB_CCDB=0 PUB_DATA=0 PUB_CERT=0 ~/publisher/get-and-run.sh
