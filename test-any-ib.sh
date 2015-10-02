#!/bin/bash -ex

hostname
which lsb_release > /dev/null 2>&1 && lsb_release -a
uname -a
date

BUILD_DATE=$(echo 2015$(echo "$(date -u +%s) / (86400 * 3)" | bc))

MIRROR=/build/mirror
WORKAREA=/build/workarea/sw/$BUILD_DATE
WORKAREA_INDEX=0

git clone https://github.com/$ALIBUILD_REPO/alibuild
git clone https://github.com/$ALIDIST_REPO/alidist

pushd alibuild
  git checkout $ALIBUILD_HASH
popd
pushd alidist
  git checkout $ALIDIST_HASH
popd

CURRENT_SLAVE=unknown
while [[ "$CURRENT_SLAVE" != '' ]]; do
  CURRENT_SLAVE=$(cat $WORKAREA/$WORKAREA_INDEX/current_slave || true)
  [[ "$CURRENT_SLAVE" == "$NODE_NAME" ]] && CURRENT_SLAVE=
  WORKAREA_INDEX=$((WORKAREA_INDEX+1))
done

mkdir -p $WORKAREA/$WORKAREA_INDEX
echo $NODE_NAME > $WORKAREA/$WORKAREA_INDEX/current_slave

for x in $OVERRIDE_TAGS; do
  OVERRIDE_PACKAGE=$(echo $x | cut -f1 -d= | tr '[:upper:]' '[:lower:]')
  OVERRIDE_TAG=$(echo $x | cut -f2 -d=)
  perl -p -i -e "s|tag: .*|tag: $OVERRIDE_TAG|" alidist/$OVERRIDE_PACKAGE.sh
done

export ALI_CI_TESTS=$TEST_TO_RUN

alibuild/aliBuild --reference-sources $MIRROR \
                  --debug \
                  --work-dir $WORKAREA/$WORKAREA_INDEX \
                  --architecture $ARCHITECTURE \
                  --jobs 16 \
                  --remote-store rsync://repo.marathon.mesos/store/ \
                  build aliroot-test || BUILDERR=$?

rm -f $WORKAREA/$WORKAREA_INDEX/current_slave
echo "Test exited with code $BUILDERR"
