#!/bin/bash -e

set -o pipefail

# Turn on printout of every build command
if [[ $EXTENDED_DEBUG ]]; then
  set -x
fi

# Swallows errors
function swallow() {
  local T=$(mktemp)
  local MSG=$1
  local ERR=0
  shift
  echo "$MSG..."
  echo "+ $*" > $T
  "$@" >> $T 2>&1 || ERR=$?
  if [[ $ERR != 0 ]]; then
    echo "Failed with exitcode $ERR, log follows"
    cat $T
    rm -f $T
    return 1
  fi
  rm -f $T
  return 0
}

# Check for required variables
for V in ALIDIST_REPO ALIBUILD_REPO PACKAGE_NAME AUTOTAG_PATTERN NODE_NAME; do
  [[ $(eval echo \$$V) ]] || { echo "Required variable $V not defined!"; ERR=1; continue; }
  eval "export $V"
done
[[ $ERR == 1 ]] && exit 1 || true

# Print some preliminary debug information
pushd "$(dirname "$0")" &> /dev/null
  echo "This is the daily-tags.sh script version $(git rev-parse HEAD)"
popd &> /dev/null
echo "Current date/time: $(date)"
echo "Current directory: $PWD"
echo "Running on host $(hostname -f), $(uname -a)"
echo "Output of lsb_release -a follows:"
type lsb_release &> /dev/null && lsb_release -a

# Clean up old stuff
rm -rf alibuild/ alidist/

# Determine branch from slug string: group/repo[@ref] (one can use both @ and :)
ALIBUILD_REPO=${ALIBUILD_REPO/:/@} # replace : with @
ALIDIST_REPO=${ALIDIST_REPO/:/@}   # replace : with @
ALIBUILD_BRANCH="${ALIBUILD_REPO##*@}"
ALIBUILD_REPO="${ALIBUILD_REPO%@*}"
[[ $ALIBUILD_REPO == $ALIBUILD_BRANCH ]] && ALIBUILD_BRANCH=
ALIDIST_BRANCH="${ALIDIST_REPO##*@}"
ALIDIST_REPO="${ALIDIST_REPO%@*}"
[[ $ALIDIST_REPO == $ALIDIST_BRANCH ]] && ALIDIST_BRANCH=

# Correctly interpret <latest> in alidist version specification
swallow "Cloning alidist" git clone https://github.com/$ALIDIST_REPO alidist/
if [[ $ALIDIST_BRANCH == *'<latest>'* ]]; then
  ALIDIST_BRANCH=${ALIDIST_BRANCH/<latest>/[0-9a-zA-Z_-]\+}
  ALIDIST_BRANCH=$(cd alidist; git log --date-order --graph --tags --simplify-by-decoration --pretty=format:'%d' | sed -e 's/tag://g' | grep -oE "$ALIDIST_BRANCH" | head -n1)
  [[ $ALIDIST_BRANCH ]] || { echo "Cannot find latest tag matching expression!"; exit 1; }
  echo "alidist <latest> tag is $ALIDIST_BRANCH"
fi
pushd alidist &> /dev/null
  swallow "Checking out alidist branch $ALIDIST_BRANCH" git checkout $ALIDIST_BRANCH
popd &> /dev/null

echo "Will be using alidist from $ALIDIST_REPO${ALIDIST_BRANCH:+, branch $ALIDIST_BRANCH}"
echo "Will be using aliBuild from $ALIBUILD_REPO${ALIBUILD_BRANCH:+, branch $ALIBUILD_BRANCH}"

# Get aliBuild with pip in a temporary directory. Gets all dependencies too
export PYTHONUSERBASE=$(mktemp -d)
export PATH=$PYTHONUSERBASE/bin:$PATH
export LD_LIBRARY_PATH=$PYTHONUSERBASE/lib:$LD_LIBRARY_PATH
swallow "Installing aliBuild" pip install --user git+https://github.com/${ALIBUILD_REPO}${ALIBUILD_BRANCH:+@$ALIBUILD_BRANCH}
type aliBuild

PACKAGE_LOWER=$(echo $PACKAGE_NAME | tr '[[:upper:]]' '[[:lower:]]')
RECIPE=alidist/$PACKAGE_LOWER.sh
AUTOTAG_REMOTE=$(grep -E '^(source:|write_repo:)' $RECIPE | sort -r | head -n1 | cut -d: -f2- | xargs echo)
AUTOTAG_MIRROR=$MIRROR/$PACKAGE_LOWER
[[ $AUTOTAG_PATTERN ]] || { echo "FATAL: A tag pattern (AUTOTAG_PATTERN) must be defined."; exit 1; }
AUTOTAG_TAG=$(LANG=C TZ=Europe/Rome date +"$AUTOTAG_PATTERN")
[[ "$TEST_TAG" == "true" ]] && AUTOTAG_TAG=TEST-IGNORE-$AUTOTAG_TAG
echo "A Git tag will be created, upon success and if not existing, with the name $AUTOTAG_TAG"
AUTOTAG_BRANCH=rc/$AUTOTAG_TAG
echo "A Git branch will be created to pinpoint the build operation, with the name $AUTOTAG_BRANCH"
AUTOTAG_CLONE=$PWD/$PACKAGE_LOWER.git

[[ -d $AUTOTAG_MIRROR ]] || AUTOTAG_MIRROR=
rm -rf $AUTOTAG_CLONE
mkdir $AUTOTAG_CLONE
pushd $AUTOTAG_CLONE &> /dev/null
  [[ -e ../git-creds ]] || git config --global credential.helper "store --file ~/git-creds-autotag"  # backwards compat
  swallow "Cloning $PACKAGE_NAME from ${AUTOTAG_REMOTE}${AUTOTAG_MIRROR:+, using mirror from $AUTOTAG_MIRROR}" \
  git clone --bare                                         \
            ${AUTOTAG_MIRROR:+--reference=$AUTOTAG_MIRROR} \
            $AUTOTAG_REMOTE .
  AUTOTAG_HASH=$( (git ls-remote 2> /dev/null | grep refs/tags/$AUTOTAG_TAG || true) | tail -n1 | awk '{print $1}' )
  if [[ "$AUTOTAG_HASH" != '' ]]; then
    echo "Tag $AUTOTAG_TAG exists already as $AUTOTAG_HASH, using it"
    AUTOTAG_ORIGIN=tag
  else
    # Tag does not exist. Create release candidate branch, if not existing.

    AUTOTAG_HASH=$( (git ls-remote 2> /dev/null | grep refs/heads/$AUTOTAG_BRANCH || true) | tail -n1 | awk '{print $1}' )
    AUTOTAG_ORIGIN=rcbranch

    if [[ "$AUTOTAG_HASH" != '' && "$REMOVE_RC_BRANCH_FIRST" == true ]]; then
      # Remove branch first if requested. Error is fatal.
      swallow "Removing existing release candidate branch $AUTOTAG_BRANCH first" git push origin :refs/heads/$AUTOTAG_BRANCH
      AUTOTAG_HASH=
    fi

    if [[ ! $AUTOTAG_HASH ]]; then
      # Let's point it to HEAD
      AUTOTAG_HASH=$( (git ls-remote 2> /dev/null | sed -e 's/\t/ /g' | grep -E ' HEAD$' || true) | tail -n1 | awk '{print $1}' )
      [[ $AUTOTAG_HASH ]] || { echo "FATAL: Cannot find any hash pointing to HEAD (repo's default branch)!" >&2; exit 1; }
      echo "Head of $AUTOTAG_REMOTE will be used, it's at $AUTOTAG_HASH"
      AUTOTAG_ORIGIN=HEAD
    fi

  fi

  # At this point, we have $AUTOTAGH_HASH for sure. It might come from HEAD, an existing rc/* branch,
  # or an existing tag. We always create a new branch out of it
  swallow "Creating remote branch $AUTOTAG_BRANCH from $AUTOTAG_HASH (hash coming from $AUTOTAG_ORIGIN)" \
    git push origin +$AUTOTAG_HASH:refs/heads/$AUTOTAG_BRANCH

popd &> /dev/null  # exit Git repo

# Select build directory in order to prevent conflicts and allow for cleanups. NODE_NAME is defined
# by Jenkins
BUILD_DATE=2015$(( $(date --utc +%s) / (86400 * 3) ))
MIRROR=/build/mirror
WORKAREA=/build/workarea/sw/$BUILD_DATE
WORKAREA_INDEX=0
CURRENT_SLAVE=unknown
while [[ "$CURRENT_SLAVE" != '' ]]; do
  WORKAREA_INDEX=$((WORKAREA_INDEX+1))
  CURRENT_SLAVE=$(cat $WORKAREA/$WORKAREA_INDEX/current_slave 2> /dev/null || true)
  [[ "$CURRENT_SLAVE" == "$NODE_NAME" ]] && CURRENT_SLAVE=
done
mkdir -p $WORKAREA/$WORKAREA_INDEX
echo $NODE_NAME > $WORKAREA/$WORKAREA_INDEX/current_slave
echo "Locking current working directory for us: $WORKAREA/$WORKAREA_INDEX"

: ${DEFAULTS:=release}

# Process overrides by changing in-place the given defaults. This requires some
# YAML processing so we are better off with Python.
env AUTOTAG_BRANCH=$AUTOTAG_BRANCH \
    PACKAGE_NAME=$PACKAGE_NAME     \
    DEFAULTS=$DEFAULTS             \
python <<\EOF
import yaml
from os import environ
f = "alidist/defaults-%s.sh" % environ["DEFAULTS"].lower()
p = environ["PACKAGE_NAME"]
d = yaml.safe_load(open(f).read().split("---")[0])
open(f+".old", "w").write(yaml.dump(d)+"\n---\n")
d["overrides"] = d.get("overrides", {})
d["overrides"][p] = d["overrides"].get(p, {})
d["overrides"][p]["tag"] = environ["AUTOTAG_BRANCH"]
open(f, "w").write(yaml.dump(d)+"\n---\n")
EOF

echo "Listing differences applied to the selected default $DEFAULTS"
DEFAULTS_LOWER=$(echo $DEFAULTS | tr '[[:upper:]]' '[[:lower:]]')
ERR=0
diff -rupN alidist/defaults-${DEFAULTS_LOWER}.sh.old alidist/defaults-${DEFAULTS_LOWER}.sh || ERR=$?
[[ $ERR == 0 || $ERR == 1 ]] || { echo "FATAL: cannot run diff"; exit 1; }

REMOTE_STORE="rsync://repo.marathon.mesos/store/::rw"
FETCH_REPOS="$(aliBuild build --help 2> /dev/null | grep fetch-repos || true)"
JOBS=8
[[ $MESOS_QUEUE_SIZE == huge ]] && JOBS=30
echo "Now running aliBuild on $JOBS parallel workers"
[[ $EXTENDED_DEBUG ]] || set -x
aliBuild --reference-sources $MIRROR                   \
         --debug                                       \
         --work-dir $WORKAREA/$WORKAREA_INDEX          \
         ${ARCHITECTURE:+--architecture $ARCHITECTURE} \
         --jobs 16                                     \
         ${FETCH_REPOS:+--fetch-repos}                 \
         --remote-store $REMOTE_STORE                  \
         ${DEFAULTS:+--defaults $DEFAULTS}             \
         build $PACKAGE_NAME || BUILDERR=$?
[[ $EXTENDED_DEBUG ]] || set +x

echo "Cleaning up temporary directory and unloking working directory"
rm -f $WORKAREA/$WORKAREA_INDEX/current_slave
rm -rf $PYTHONUSERBASE
[[ "$BUILDERR" != '' ]] && { echo "Exiting with an error ($BUILDERR), not tagging"; exit $BUILDERR; }

# Now we tag, in case we should
pushd $AUTOTAG_CLONE &> /dev/null
  if [[ $AUTOTAG_ORIGIN != tag ]]; then
    swallow "Tagging $AUTOTAG_TAG from $AUTOTAG_HASH" git push origin +$AUTOTAG_HASH:refs/tags/$AUTOTAG_TAG
  else
    echo "Not tagging: tag $AUTOTAG_TAG exists already as $AUTOTAG_HASH"
  fi
  swallow "Removing working branch $AUTOTAG_BRANCH" git push origin :refs/heads/$AUTOTAG_BRANCH || true  # error is not a big deal here
popd &> /dev/null

echo All OK
