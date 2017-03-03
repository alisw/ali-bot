#!/bin/bash -ex

while true; do
  report-metric-monalisa --monalisa-host aliendb9.cern.ch                       \
                         --monalisa-port 8885                                   \
                         --metric-path github-pr-checker/github.com             \
                         --metric-name github-api-calls                         \
                         --metric-value `monitor-github-api 2>&1 | cut -f2 -d,`
   sleep 10
done
