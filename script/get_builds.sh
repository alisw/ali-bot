#!/bin/bash
curl -X POST http://elasticsearch.marathon.mesos:9200/_search/ -d@es/build_results.json
