#!/bin/bash -x
function report_state() {
  CURRENT_STATE=$1
  # Push some metric about being up and running to Monalisa
  $TIMEOUT_CMD report-metric-monalisa --metric-path github-pr-checker.${CI_NAME:+$CI_NAME}_Nodes/$ALIBOT_ANALYTICS_USER_UUID \
                                      --metric-name state                                                                    \
                                      --metric-value $CURRENT_STATE
  $TIMEOUT_CMD report-analytics screenview --cd $CURRENT_STATE
  # Calculate PR statistics
  TIME_NOW=$(date -u +%s)
  PRTIME=
  [[ $CURRENT_STATE == pr_processing ]] && TIME_PR_STARTED=$TIME_NOW
  [[ $CURRENT_STATE == pr_processing_done ]] && PRTIME="$((TIME_NOW-TIME_PR_STARTED))"

  # Push to InfluxDB if configured
  if [[ $INFLUXDB_WRITE_URL ]]; then
    DATA="prcheck,checkname=$CHECK_NAME/$WORKER_INDEX host=\"$(hostname -s)\",state=\"$CURRENT_STATE\",cihash=\"$CI_HASH\",uptime=$((TIME_NOW-TIME_STARTED))${PRTIME:+,prtime=${PRTIME}}${LAST_PR:+,prid=\"$LAST_PR\"}${LAST_PR_OK:+,prok=$LAST_PR_OK} $((TIME_NOW*1000000000))"
    curl $INFLUX_INSECURE --max-time 20 -XPOST "$INFLUXDB_WRITE_URL" --data-binary "$DATA" || true
  fi

  # Push to Google Analytics if configured
  if [ X${ALIBOT_ANALYTICS_ID:+1} = X1 ]; then
    # Report first PR and the rest in a separate category
    [ ! X$PRTIME = X ] && $TIMEOUT_CMD report-analytics timing --utc "${ONESHOT:+First }PR Building" --utv "time" --utt $((PRTIME * 1000)) --utl $CHECK_NAME/$WORKER_INDEX
  fi
}
