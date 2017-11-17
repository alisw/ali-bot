#!/bin/bash -ex

hostname
which lsb_release > /dev/null 2>&1 && lsb_release -a
uname -a
date

BUILD_DATE=$(echo 2015$(echo "$(date -u +%s) / (86400 * 3)" | bc))

MIRROR=/build/mirror
WORKAREA=/build/workarea/sw/$BUILD_DATE
WORKAREA_INDEX=0

# Backwards compatibility (run standalone with old interface and pipelines too)
[[ $ALIBUILD_REPO == */* ]] || ALIBUILD_REPO=$ALIBUILD_REPO/alibuild
[[ $ALIDIST_REPO == */* ]] || ALIDIST_REPO=$ALIDIST_REPO/alidist
rm -rf alibuild/ alidist/

# Correctly interpret <latest> in alidist version specification
git clone https://github.com/$ALIDIST_REPO alidist/
if [[ $ALIDIST_BRANCH == *'<latest>'* ]]; then
  ALIDIST_BRANCH=${ALIDIST_BRANCH/<latest>/[0-9a-zA-Z_-]\+}
  ALIDIST_BRANCH=$(cd alidist; git log --date-order --graph --tags --simplify-by-decoration --pretty=format:'%d' | grep -oE "$ALIDIST_BRANCH" | head -n1)
  #ALIDIST_BRANCH=$(cd alidist; git tag --sort=-v:refname | grep -E -- "$ALIDIST_BRANCH" | head -1)
  [[ $ALIDIST_BRANCH ]] || { echo "Cannot find latest tag matching expression!"; exit 1; }
fi
pushd alidist
  git checkout $ALIDIST_BRANCH
  exit 0
popd

# Get aliBuild with pip in a temporary directory. Gets all dependencies too
export PYTHONUSERBASE=$(mktemp -d)
export PATH=$PYTHONUSERBASE/bin:$PATH
export LD_LIBRARY_PATH=$PYTHONUSERBASE/lib:$LD_LIBRARY_PATH
pip install --user git+https://github.com/${ALIBUILD_REPO}${ALIBUILD_BRANCH:+@$ALIBUILD_BRANCH}
type aliBuild

set -o pipefail

PACKAGE_LOWER=$(echo $PACKAGE_NAME | tr '[[:upper:]]' '[[:lower:]]')
RECIPE=alidist/$PACKAGE_LOWER.sh
AUTOTAG_REMOTE=$(grep -E '^(source:|write_repo:)' $RECIPE | sort -r | head -n1 | cut -d: -f2- | xargs echo)
AUTOTAG_MIRROR=$MIRROR/$PACKAGE_LOWER
AUTOTAG_TAG=vAN-$(LANG=C date +%Y%m%d)
[[ "$TEST_TAG" == "true" ]] && AUTOTAG_TAG=TEST-IGNORE-$AUTOTAG_TAG
AUTOTAG_BRANCH=rc/$AUTOTAG_TAG
AUTOTAG_REF=$AUTOTAG_BRANCH
AUTOTAG_CLONE=$PWD/$PACKAGE_LOWER.git
[[ -d $AUTOTAG_MIRROR ]] || AUTOTAG_MIRROR=
rm -rf $AUTOTAG_CLONE
mkdir $AUTOTAG_CLONE
pushd $AUTOTAG_CLONE
  [[ -e ../git-creds ]] || git config --global credential.helper "store --file ~/git-creds-autotag"  # backwards compat
  git clone --bare                                         \
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

# Process overrides by changing in-place the given defaults. This requires some
# YAML processing so we are better off with Python.
env AUTOTAG_BRANCH=$AUTOTAG_BRANCH python <<\EOF
import yaml
from os import environ
f = "alidist/defaults-%s.sh" % environ["DEFAULTS"].lower()
p = environ["PACKAGE_NAME"]
d = yaml.safe_load(open(f).read().split("---")[0])
d["overrides"] = d.get("overrides", {})
d["overrides"][p] = d["overrides"].get(p, {})
d["overrides"][p]["tag"] = environ["AUTOTAG_BRANCH"]
open(f, "w").write(yaml.dump(d)+"\n---\n")
EOF
pushd alidist
  git diff
popd

REMOTE_STORE="rsync://repo.marathon.mesos/store/::rw"
aliBuild --reference-sources $MIRROR                   \
         --debug                                       \
         --work-dir $WORKAREA/$WORKAREA_INDEX          \
         --architecture $ARCHITECTURE                  \
         --jobs 16                                     \
         --remote-store $REMOTE_STORE                  \
         ${DEFAULTS:+--defaults $DEFAULTS}             \
         build $PACKAGE_NAME || BUILDERR=$?

rm -f $WORKAREA/$WORKAREA_INDEX/current_slave
rm -rf $PYTHONUSERBASE
[[ "$BUILDERR" != '' ]] && exit $BUILDERR

# Now we tag, in case we should
pushd $AUTOTAG_CLONE
  [[ "$AUTOTAG_TAG" == "$AUTOTAG_REF" ]] || git push origin $AUTOTAG_HASH:refs/tags/$AUTOTAG_TAG
  git push origin :refs/heads/$AUTOTAG_BRANCH || true  # error is not a big deal here
popd
