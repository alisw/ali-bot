#!/bin/bash -ex

hostname
echo $RIEMANN_HOST
which lsb_release > /dev/null 2>&1 && lsb_release -a
uname -a
date

BUILD_DATE=$(echo 2015$(echo "$(date -u +%s) / (86400 * 3)" | bc))

MIRROR=/build/mirror

# Allow for $WORKAREA to be overridden so that we can build special
# builds (e.g. coverage ones) in a different PATH.
WORKAREA=${WORKAREA:-/build/workarea/sw/$BUILD_DATE}
WORKAREA_INDEX=0

# Get aliBuild with pip in a temporary directory. Gets all dependencies too
export PYTHONUSERBASE=$(mktemp -d)
export PATH=$PYTHONUSERBASE/bin:$PATH
export LD_LIBRARY_PATH=$PYTHONUSERBASE/lib:$LD_LIBRARY_PATH
pip install --user git+https://github.com/$ALIBUILD_REPO/alibuild${ALIBUILD_BRANCH:+@$ALIBUILD_BRANCH}
type aliBuild

if [[ $ALIDIST_BRANCH =~ pull/ ]]; then
  git clone https://github.com/$ALIDIST_REPO/alidist
  pushd alidist
    ALIDIST_LOCAL_BRANCH=$(echo $ALIDIST_BRANCH|sed -e 's|/|_|g')
    git fetch origin $ALIDIST_BRANCH:$ALIDIST_LOCAL_BRANCH
    git checkout $ALIDIST_LOCAL_BRANCH
  popd
else
  git clone -b $ALIDIST_BRANCH https://github.com/$ALIDIST_REPO/alidist
fi

CURRENT_SLAVE=unknown
while [[ "$CURRENT_SLAVE" != '' ]]; do
  WORKAREA_INDEX=$((WORKAREA_INDEX+1))
  CURRENT_SLAVE=$(cat $WORKAREA/$WORKAREA_INDEX/current_slave 2> /dev/null || true)
  [[ "$CURRENT_SLAVE" == "$NODE_NAME" ]] && CURRENT_SLAVE=
done

mkdir -p $WORKAREA/$WORKAREA_INDEX
echo $NODE_NAME > $WORKAREA/$WORKAREA_INDEX/current_slave

for x in $OVERRIDE_TAGS; do
  OVERRIDE_PACKAGE=$(echo $x | cut -f1 -d= | tr '[:upper:]' '[:lower:]')
  OVERRIDE_TAG=$(echo $x | cut -f2 -d=)
  perl -p -i -e "s|tag: .*|tag: $OVERRIDE_TAG|" alidist/$OVERRIDE_PACKAGE.sh
done
for x in $OVERRIDE_VERSIONS; do
  OVERRIDE_PACKAGE=$(echo $x | cut -f1 -d= | tr '[:upper:]' '[:lower:]')
  OVERRIDE_VERSION=$(echo $x | cut -f2 -d=)
  perl -p -i -e "s|version: .*|version: $OVERRIDE_VERSION|" alidist/$OVERRIDE_PACKAGE.sh
done
( cd alidist && git diff )

# Allow to specify AliRoot and AliPhysics as a development packages
if [[ "$ALIROOT_DEVEL_VERSION" != '' ]]; then
  aliBuild init AliRoot@$ALIROOT_DEVEL_VERSION
  pushd AliRoot
    # Either pull changes in the branch or reset it to the requested tag
    git pull origin || git reset --hard $ALIROOT_DEVEL_VERSION
  popd
else
  rm -rf AliRoot
fi
if [[ "$ALIPHYSICS_DEVEL_VERSION" != '' ]]; then
  aliBuild init AliPhysics@$ALIPHYSICS_DEVEL_VERSION
  pushd AliPhysics
    # Either pull changes in the branch or reset it to the requested tag
    git pull origin || git reset --hard $ALIPHYSICS_DEVEL_VERSION
  popd
else
  rm -rf AliPhysics
fi

RWOPT='::rw'
[[ "$PUBLISH_BUILDS" == "false" ]] && RWOPT=
REMOTE_STORE="${REMOTE_STORE:-rsync://repo.marathon.mesos/store/}$RWOPT"
[[ "$USE_REMOTE_STORE" == "false" ]] && REMOTE_STORE=
aliBuild --reference-sources $MIRROR                    \
         --debug                                        \
         --work-dir $WORKAREA/$WORKAREA_INDEX           \
         --architecture $ARCHITECTURE                   \
         --jobs 16                                      \
         ${REMOTE_STORE:+--remote-store $REMOTE_STORE}  \
         ${DEFAULTS:+--defaults $DEFAULTS}              \
         ${DISABLE:+--disable $DISABLE}                 \
         build $PACKAGE_NAME || BUILDERR=$?

rm -f $WORKAREA/$WORKAREA_INDEX/current_slave
[[ "$BUILDERR" != '' ]] && exit $BUILDERR

echo ALIROOT_BUILD_NR=$BUILD_NUMBER >> results.props
echo PACKAGE_NAME=$PACKAGE_NAME >> results.props

ALIDIST_HASH=$(cd $WORKSPACE/alidist && git rev-parse HEAD)
ALIBUILD_HASH=$(aliBuild version 2> /dev/null || true)
rm -rf $PYTHONUSERBASE

case $PACKAGE_NAME in
  aliroot*|zlib*)
for x in gun ppbench PbPbbench; do
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
