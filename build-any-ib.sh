#!/bin/bash -ex

hostname
which lsb_release > /dev/null 2>&1 && lsb_release -a
uname -a
date

BUILD_DATE=$(echo 2015$(echo "$(date -u +%s) / (86400 * 3)" | bc))

MIRROR=/build/mirror
WORKAREA=/build/workarea/sw/$BUILD_DATE
WORKAREA_INDEX=0

git clone -b $ALIBUILD_BRANCH https://github.com/$ALIBUILD_REPO/alibuild
git clone -b $ALIDIST_BRANCH https://github.com/$ALIDIST_REPO/alidist

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

RWOPT='::rw'
[[ "$PUBLISH_BUILDS" == "false" ]] && RWOPT=''
alibuild/aliBuild --reference-sources $MIRROR \
                  --debug \
                  --work-dir $WORKAREA/$WORKAREA_INDEX \
                  --architecture $ARCHITECTURE \
                  --jobs 16 \
                  --remote-store rsync://repo.marathon.mesos/store/$RWOPT \
                  build $PACKAGE_NAME || BUILDERR=$?

rm -f $WORKAREA/$WORKAREA_INDEX/current_slave
[[ "$BUILDERR" != '' ]] && exit $BUILDERR

echo ALIROOT_BUILD_NR=$BUILD_NUMBER >> results.props
echo PACKAGE_NAME=$PACKAGE_NAME >> results.props

ALIDIST_HASH=$(cd $WORKSPACE/alidist && git show-ref HEAD | cut -f1 -d\ )
ALIBUILD_HASH=$(cd $WORKSPACE/alibuild && git show-ref HEAD | cut -f1 -d\ )

case $PACKAGE_NAME in
  aliroot*|zlib*)
for x in gun ppbench; do
cat << EOF > $x-tests.props
ALIROOT_BUILD_NR=$BUILD_NUMBER
PACKAGE_NAME=$PACKAGE_NAME
ALIDIST_HASH=$ALIDIST_HASH
ALIBUILD_HASH=$ALIBUILD_HASH
TEST_TO_RUN=$x
BUILD_DATE=$BUILD_DATE
EOF
done
  ;;
esac
