#!/bin/bash -e
NUM_PRS=${NUM_PRS:-20}
GH_USER=${GH_USER:-dberzano}
[[ $DRY_RUN ]] && DRY='echo Would run: ' || true
type hub &> /dev/null
$DRY git clean -fxd
$DRY git fetch --all
CHANGE_FILES=$(find . -name "*.cxx" | sort -R | tail -$NUM_PRS)
COUNT=0
for FILE in ${CHANGE_FILES[@]}; do
  COUNT=$((COUNT+1))
  $DRY git checkout master
  $DRY git reset --hard origin/master
  $DRY git branch -D stresstest-$COUNT || true
  $DRY git checkout -b stresstest-$COUNT
  [[ ! $DRY_RUN ]] || echo "// this comment serves no purpose" >> $FILE
  $DRY git commit -a -m "Commit for Stress Test $COUNT"
  $DRY git push -f --set-upstream dberzano stresstest-$COUNT
  $DRY hub pull-request -m "Stress Test $COUNT"
done
