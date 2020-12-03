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

git config --global user.name 'ALICE Builder'
git config --global user.email alibuild@cern.ch

git clone -b $ALIDIST_BRANCH https://github.com/$ALIDIST_REPO alidist/

# Install the latest release if ALIBUILD_SLUG is not provided
pip install --user --ignore-installed --upgrade ${ALIBUILD_SLUG:+git+https://github.com/}${ALIBUILD_SLUG:-alibuild}

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
meta, rest = open(f).read().split("\n---\n", 1)
d = yaml.safe_load(meta)
open(f+".old", "w").write(yaml.dump(d)+"\n---\n"+rest)
d["overrides"] = d.get("overrides", {})
d["overrides"][p] = d["overrides"].get(p, {})
d["overrides"][p]["tag"] = environ["AUTOTAG_BRANCH"]
v = environ.get("AUTOTAG_OVERRIDE_VERSION")
if v:
    d["overrides"][p]["version"] = v
open(f, "w").write(yaml.dump(d)+"\n---\n"+rest)
EOF

diff -rupN alidist/defaults-${DEFAULTS_LOWER}.sh.old alidist/defaults-${DEFAULTS_LOWER}.sh | cat

# Select build directory in order to prevent conflicts and allow for cleanups.
workarea=$(mktemp -d "$PWD/daily-tags.XXXXXXXXXX")

REMOTE_STORE="${REMOTE_STORE:-rsync://repo.marathon.mesos/store/::rw}"
JOBS=8
[[ $MESOS_QUEUE_SIZE == huge ]] && JOBS=30
echo "Now running aliBuild on $JOBS parallel workers"
aliBuild --reference-sources mirror                    \
         --debug                                       \
         --work-dir "$workarea"                        \
         ${ARCHITECTURE:+--architecture $ARCHITECTURE} \
         --jobs 10                                     \
         --fetch-repos                                 \
         --remote-store $REMOTE_STORE                  \
         ${DEFAULTS:+--defaults $DEFAULTS}             \
         build "$PACKAGE_NAME" || {
  builderr=$?
  echo "Exiting with an error ($builderr), not tagging"
  exit $builderr
}

# Now we tag, in case we should
pushd $AUTOTAG_CLONE &> /dev/null
  if [[ $AUTOTAG_ORIGIN != tag ]]; then
    git push origin +$AUTOTAG_HASH:refs/tags/$AUTOTAG_TAG
  else
    echo "Not tagging: tag $AUTOTAG_TAG exists already as $AUTOTAG_HASH"
  fi
  git push origin :refs/heads/$AUTOTAG_BRANCH || true  # error is not a big deal here
popd &> /dev/null

# Also tag the appropriate alidist
cd alidist
defaults_fname=defaults-${DEFAULTS,,}.sh
# If the file was modified, the output of git status will be non-empty.
if [ -n "$(git status --porcelain=v1 "$defaults_fname")" ]; then
  git add "$defaults_fname"
  git commit -m "Auto-update $defaults_fname"
fi
git push origin "HEAD:refs/tags/${PACKAGE_NAME:?}-${AUTOTAG_TAG:?}"
