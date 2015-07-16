#!/bin/bash
curl -X POST http://elasticsearch.marathon.mesos:9200/_search/ -d@es/build_results.json > $WORKSPACE/alisw.github.io/_data/build.json
curl -X POST "http://elasticsearch.marathon.mesos:9200/_search/?pretty=true&filter_path=**.key" -d@es/build_summary.json > $WORKSPACE/alisw.github.io/_data/build_summary.json
