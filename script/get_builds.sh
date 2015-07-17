#!/bin/bash
ES_HOST=elasticsearch.marathon.mesos:9200
PROXY=${PROXY+-x socks5h://$PROXY}
TARGET=${TARGET-$WORKSPACE/alisw.github.io}
curl -X POST "http://$ES_HOST/_search/?pretty=true&filter_path=**.key" -d@es/build_results.json > $TARGET/_data/build.json
curl -X POST "http://$ES_HOST/_search/?pretty=true&filter_path=**.key" -d@es/build_summary.json > $TARGET/_data/build_summary.json
curl -X POST "http://$ES_HOST/_search/?pretty=true&filter_path=**.key" -d@es/build_errors.json > $TARGET/_data/errors.json
