#!/bin/bash -ex
set -x

# Check for required variables
ALIDIST_SLUG=${ALIDIST_SLUG:-alisw/alidist@master}
[ ! -z "$PACKAGE_NAME" ]
[ ! -z "$AUTOTAG_PATTERN" ]
[ ! -z "$NODE_NAME" ]

# Clean up old stuff
rm -rf alidist/

# Determine branch from slug string: group/repo@ref
ALIDIST_BRANCH="${ALIDIST_SLUG##*@}"
ALIDIST_REPO="${ALIDIST_SLUG%@*}"

# Correctly interpret <latest> in alidist version specification
if [[ $ALIDIST_BRANCH == *'<latest>'* ]]; then
  ALIDIST_BRANCH=${ALIDIST_BRANCH/<latest>/[0-9a-zA-Z_-]\+}
  ALIDIST_BRANCH=$(GIT_DIR=alidist/.git git log --date-order --graph --tags --simplify-by-decoration --pretty=format:'%d' | sed -e 's/tag://g' | grep -m 1 -oE "$ALIDIST_BRANCH")
  [[ $ALIDIST_BRANCH ]] || { echo "Cannot find latest tag matching expression!"; exit 1; }
fi

git clone -b $ALIDIST_BRANCH https://github.com/$ALIDIST_REPO alidist/

# Install the latest release if ALIBUILD_SLUG is not provided
pip install --user --ignore-installed --upgrade ${ALIBUILD_SLUG:-alibuild}${ALIBUILD_SLUG:+git+https://github.com/$ALIBUILD_SLUG}

PACKAGE_LOWER=$(echo $PACKAGE_NAME | tr '[[:upper:]]' '[[:lower:]]')
RECIPE=alidist/$PACKAGE_LOWER.sh
AUTOTAG_REMOTE=$(grep -E '^(source:|write_repo:)' $RECIPE | sort -r | head -n1 | cut -d: -f2- | xargs echo)
AUTOTAG_MIRROR=$MIRROR/$PACKAGE_LOWER
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
  git clone --bare                                         \
            ${AUTOTAG_MIRROR:+--reference=$AUTOTAG_MIRROR} \
            $AUTOTAG_REMOTE .
  AUTOTAG_HASH=$( (git ls-remote 2> /dev/null | grep refs/tags/$AUTOTAG_TAG || true) | awk 'END{print $1}' )
  if [[ "$AUTOTAG_HASH" != '' ]]; then
    echo "Tag $AUTOTAG_TAG exists already as $AUTOTAG_HASH, using it"
    AUTOTAG_ORIGIN=tag
  elif [[ $DO_NOT_CREATE_NEW_TAG == true ]]; then
    # Tag does not exist, but we have requested this job to forcibly use an existing one.
    # Will abort the job.
    echo "Tag $AUTOTAG_TAG was not found, however we have been requested to not create a new one" \
         "(DO_NOT_CREATE_NEW_TAG is true). Aborting with error"
    exit 1
  else
    # Tag does not exist. Create release candidate branch, if not existing.

    AUTOTAG_HASH=$( (git ls-remote 2> /dev/null | grep refs/heads/$AUTOTAG_BRANCH || true) | awk 'END{print $1}' )
    AUTOTAG_ORIGIN=rcbranch

    if [[ "$AUTOTAG_HASH" != '' && "$REMOVE_RC_BRANCH_FIRST" == true ]]; then
      # Remove branch first if requested. Error is fatal.
      git push origin :refs/heads/$AUTOTAG_BRANCH
      AUTOTAG_HASH=
    fi

    if [[ ! $AUTOTAG_HASH ]]; then
      # Let's point it to HEAD
      AUTOTAG_HASH=$( (git ls-remote 2> /dev/null | sed -e 's/\t/ /g' | grep -E ' HEAD$' || true) | awk 'END{print $1}' )
      [[ $AUTOTAG_HASH ]] || { echo "FATAL: Cannot find any hash pointing to HEAD (repo's default branch)!" >&2; exit 1; }
      echo "Head of $AUTOTAG_REMOTE will be used, it's at $AUTOTAG_HASH"
      AUTOTAG_ORIGIN=HEAD
    fi

  fi

  # At this point, we have $AUTOTAGH_HASH for sure. It might come from HEAD, an existing rc/* branch,
  # or an existing tag. We always create a new branch out of it
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
v = environ.get("AUTOTAG_OVERRIDE_VERSION")
if v:
    d["overrides"][p]["version"] = v
open(f, "w").write(yaml.dump(d)+"\n---\n")
EOF

diff -rupN alidist/defaults-${DEFAULTS_LOWER}.sh.old alidist/defaults-${DEFAULTS_LOWER}.sh | cat

REMOTE_STORE="${REMOTE_STORE:-rsync://repo.marathon.mesos/store/::rw}"
JOBS=8
[[ $MESOS_QUEUE_SIZE == huge ]] && JOBS=30
echo "Now running aliBuild on $JOBS parallel workers"
aliBuild --reference-sources $MIRROR                   \
         --debug                                       \
         --work-dir $WORKAREA/$WORKAREA_INDEX          \
         ${ARCHITECTURE:+--architecture $ARCHITECTURE} \
         --jobs 16                                     \
         --fetch-repos                                 \
         --remote-store $REMOTE_STORE                  \
         ${DEFAULTS:+--defaults $DEFAULTS}             \
         build $PACKAGE_NAME || BUILDERR=$?

rm -rf $WORKAREA/$WORKAREA_INDEX/current_slave
[[ "$BUILDERR" != '' ]] && { echo "Exiting with an error ($BUILDERR), not tagging"; exit $BUILDERR; }

# Now we tag, in case we should
pushd $AUTOTAG_CLONE &> /dev/null
  if [[ $AUTOTAG_ORIGIN != tag ]]; then
    git push origin +$AUTOTAG_HASH:refs/tags/$AUTOTAG_TAG
  else
    echo "Not tagging: tag $AUTOTAG_TAG exists already as $AUTOTAG_HASH"
  fi
  git push origin :refs/heads/$AUTOTAG_BRANCH || true  # error is not a big deal here
popd &> /dev/null
