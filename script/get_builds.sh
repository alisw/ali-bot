#!/bin/bash
ES_HOST=elasticsearch-client.marathon.mesos:9200
PROXY=${PROXY+-x socks5h://$PROXY}
TARGET="${TARGET:-$WORKSPACE/alisw.github.io}"
echo $TARGET
curl -X POST "http://$ES_HOST/_search/?pretty=true&filter_path=**.key" -d@es/build_results.json > "$TARGET/_data/build.json"
curl -X POST "http://$ES_HOST/_search/?pretty=true&filter_path=**.key" -d@es/build_summary.json > "$TARGET/_data/build_summary.json"
curl -X POST "http://$ES_HOST/_search/?pretty=true&filter_path=**.key" -d@es/build_errors.json > "$TARGET/_data/errors.json"
curl -X POST "http://$ES_HOST/_search/?pretty=true&filter_path=**.key" -d@es/build_warnings.json > "$TARGET/_data/warnings.json"
curl -X POST "http://$ES_HOST/_search/?pretty=true&filter_path=**.key" -d@es/build_status.json > "$TARGET/_data/status.json"
curl -X POST "http://$ES_HOST/_search/?pretty=true&filter_path=**.key" -d@es/build_tests_status.json > "$TARGET/_data/tests_status.json"
curl -X POST "http://$ES_HOST/_search/?pretty=true" -d@es/build_tools_info.json | grep -v "\"took\" :" > "$TARGET/_data/tools.json"
