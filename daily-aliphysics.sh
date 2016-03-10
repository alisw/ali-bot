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

set -o pipefail
AUTOTAG_REMOTE=https://git.cern.ch/reps/AliPhysics
AUTOTAG_MIRROR=$MIRROR/aliphysics
AUTOTAG_TAG=vAN-$(LANG=C date +%Y%m%d)
[[ "$TEST_TAG" == "true" ]] && AUTOTAG_TAG=TEST-IGNORE-$AUTOTAG_TAG
AUTOTAG_BRANCH=rc/$AUTOTAG_TAG
AUTOTAG_REF=$AUTOTAG_BRANCH
AUTOTAG_CLONE=$PWD/aliphysics.git
[[ -d $AUTOTAG_MIRROR ]] || AUTOTAG_MIRROR=
rm -rf $AUTOTAG_CLONE
mkdir $AUTOTAG_CLONE
pushd $AUTOTAG_CLONE
  git config --global credential.helper "store --file ~/git-creds-autotag"
  git clone --bare \
            ${AUTOTAG_MIRROR:+--reference=$AUTOTAG_MIRROR} \
            $AUTOTAG_REMOTE .
  AUTOTAG_HASH=$( (git ls-remote | grep refs/tags/$AUTOTAG_TAG || true) | tail -n1 | awk '{print $1}' )
  if [[ "$AUTOTAG_HASH" != '' ]]; then
    # Tag exists. Use it.
    # NOTE: TAG==REF is the condition for *not* creating the tag afterwards.
    AUTOTAG_REF=$AUTOTAG_TAG
    echo "Tag $AUTOTAG_TAG exists already as $AUTOTAG_HASH"
  else
    # Tag does not exist. Create release candidate branch, if not existing.

    AUTOTAG_HASH=$( (git ls-remote | grep refs/heads/$AUTOTAG_BRANCH || true) | tail -n1 | awk '{print $1}' )

    if [[ "$AUTOTAG_HASH" != '' && "$REMOVE_RC_BRANCH_FIRST" == true ]]; then
      # Remove branch first if requested. Error is fatal.
      git push origin :refs/heads/$AUTOTAG_BRANCH
      AUTOTAG_HASH=
    fi

    if [[ "$AUTOTAG_HASH" == '' ]]; then
      AUTOTAG_HASH=$( (git ls-remote | grep refs/heads/master || true) | tail -n1 | awk '{print $1}' )
      [[ "$AUTOTAG_HASH" != '' ]]
      git push origin $AUTOTAG_HASH:refs/heads/$AUTOTAG_BRANCH
    fi
  fi
popd

CURRENT_SLAVE=unknown
while [[ "$CURRENT_SLAVE" != '' ]]; do
  WORKAREA_INDEX=$((WORKAREA_INDEX+1))
  CURRENT_SLAVE=$(cat $WORKAREA/$WORKAREA_INDEX/current_slave 2> /dev/null || true)
  [[ "$CURRENT_SLAVE" == "$NODE_NAME" ]] && CURRENT_SLAVE=
done

mkdir -p $WORKAREA/$WORKAREA_INDEX
echo $NODE_NAME > $WORKAREA/$WORKAREA_INDEX/current_slave

# Ordinary override, as given by user
for x in $OVERRIDE_TAGS; do
  OVERRIDE_PACKAGE=$(echo $x | cut -f1 -d= | tr '[:upper:]' '[:lower:]')
  OVERRIDE_TAG=$(echo $x | cut -f2 -d=)
  perl -p -i -e "s|tag: .*|tag: $OVERRIDE_TAG|" alidist/$OVERRIDE_PACKAGE.sh
done

# Extra override for AliPhysics
perl -p -i -e "s|version: .*|version: ${AUTOTAG_TAG}${DEFAULTS:+_$(echo ${DEFAULTS} | tr '[:lower:]' '[:upper:]')}|" alidist/aliphysics.sh
perl -p -i -e "s|tag: .*|tag: $AUTOTAG_REF|" alidist/aliphysics.sh

RWOPT='::rw'
[[ "$PUBLISH_BUILDS" == "false" ]] && RWOPT=
REMOTE_STORE="rsync://repo.marathon.mesos/store/$RWOPT"
[[ "$USE_REMOTE_STORE" == "false" ]] && REMOTE_STORE=
alibuild/aliBuild --reference-sources $MIRROR \
                  --debug \
                  --work-dir $WORKAREA/$WORKAREA_INDEX \
                  --architecture $ARCHITECTURE \
                  --jobs 16 \
                  ${REMOTE_STORE:+--remote-store $REMOTE_STORE} \
                  ${DEFAULTS:+--defaults $DEFAULTS} \
                  build $PACKAGE_NAME || BUILDERR=$?

rm -f $WORKAREA/$WORKAREA_INDEX/current_slave
[[ "$BUILDERR" != '' ]] && exit $BUILDERR

# Now we tag. In case we should.
pushd $AUTOTAG_CLONE
  [[ "$AUTOTAG_TAG" == "$AUTOTAG_REF" ]] || git push origin $AUTOTAG_HASH:refs/tags/$AUTOTAG_TAG
  git push origin :refs/heads/$AUTOTAG_BRANCH || true  # error is not a big deal here
popd

echo ALIROOT_BUILD_NR=$BUILD_NUMBER >> results.props
echo PACKAGE_NAME=$PACKAGE_NAME >> results.props

ALIDIST_HASH=$(cd $WORKSPACE/alidist && git rev-parse HEAD)
ALIBUILD_HASH=$(cd $WORKSPACE/alibuild && git rev-parse HEAD)

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
