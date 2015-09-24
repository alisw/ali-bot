#!/bin/bash
ES_HOST=elasticsearch-client.marathon.mesos:9200
PROXY=${PROXY+--proxy socks5h://$PROXY}
TARGET=${TARGET-$WORKSPACE/alisw.github.io}
curl $PROXY -X PUT "http://$ES_HOST/_mapping/logs" -d@es-mappings/logs.json
