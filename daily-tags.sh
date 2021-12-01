#!/bin/bash -ex
set -ex

# Check for required variables
ALIDIST_SLUG=${ALIDIST_SLUG:-alisw/alidist@master}
if echo "$ALIDIST_SLUG" | grep -q '!!FLPSUITE_LATEST!!'; then
  yum install -y jq
  case $(date +%u) in
    1)  # Monday (for Sunday night build)
      # Sort available flp-suite-* branches by version number, then pick the latest one.
      flpsuite_latest=$(git ls-remote "https://github.com/${ALIDIST_SLUG%@*}" 'refs/heads/flp-suite-*' |
                          sort -rVt - -k 3 | sed -rn '1s|[0-9a-f]+\trefs/heads/||p') ;;
    *)  # Tuesday-Sunday
      # Fetch the latest installed FLP suite version, but amend the patch version
      # number to .0 (as that's how the alidist branches are named).
      flpsuite_latest=$(curl -Lk http://ali-flp.cern.ch/tags | jq -r '.[0].name' | 
                        sed -e 's/.*v\([0-9.][0-9.]*\)\.[0-9].*/\1.0/') ;;
  esac
  ALIDIST_SLUG=${ALIDIST_SLUG//!!FLPSUITE_LATEST!!/$flpsuite_latest}
  unset flpsuite_latest
fi

# PACKAGES contains whitespace-separated package names to tag. Only the first is
# built, but every listed package's tag is edited in the resulting commit. This
# enables tagging e.g. O2 and O2Physics at the same time, with the same tag, and
# building O2Physics (which pulls in O2 as well).
main_pkg=${PACKAGES%% *}
[ -n "$PACKAGES" ]
[ -n "$main_pkg" ]
[ -n "$AUTOTAG_TAG" ]
[ -n "$NODE_NAME" ]

# Clean up old stuff
rm -rf alidist/

# Determine branch from slug string: group/repo@ref
ALIDIST_BRANCH="${ALIDIST_SLUG##*@}"
ALIDIST_REPO="${ALIDIST_SLUG%@*}"

git config --global user.name 'ALICE Builder'
git config --global user.email alibuild@cern.ch

git clone -b $ALIDIST_BRANCH https://github.com/$ALIDIST_REPO alidist/

# Set the default python and pip depending on the architecture...
case $ARCHITECTURE in
  slc8*) PIP=pip3 PYTHON=python3 ;;
  *) PIP=pip PYTHON=python ;;
esac
# ...and override it if PYTHON_VERSION is specified.
case "$PYTHON_VERSION" in
  2) PIP=pip2 PYTHON=python2 ;;
  3) PIP=pip3 PYTHON=python3 ;;
esac

# Install the latest release if ALIBUILD_SLUG is not provided
$PIP install --user --upgrade "${ALIBUILD_SLUG:+git+https://github.com/}${ALIBUILD_SLUG:-alibuild}"

[ "$TEST_TAG" = true ] && AUTOTAG_TAG=TEST-IGNORE-$AUTOTAG_TAG
[ -e git-creds ] || git config --global credential.helper "store --file ~/git-creds-autotag"  # backwards compat

for package in $PACKAGES; do (
  rm -rf "${package,,}.git"
  AUTOTAG_REMOTE=$(grep -E '^(source:|write_repo:)' "alidist/${package,,}.sh" | sort -r | head -n1 | cut -d: -f2- | xargs echo)
  if [ -n "$AUTOTAG_REMOTE" ]; then
    AUTOTAG_MIRROR=$MIRROR/${package,,}
    echo "A Git tag will be created, upon success and if not existing, with the name $AUTOTAG_TAG"
    echo "A Git branch will be created to pinpoint the build operation, with the name rc/$AUTOTAG_TAG"

    [[ -d $AUTOTAG_MIRROR ]] || AUTOTAG_MIRROR=
    mkdir "${package,,}.git"
    cd "${package,,}.git"
    git clone --bare ${AUTOTAG_MIRROR:+--reference=$AUTOTAG_MIRROR} "$AUTOTAG_REMOTE" .
    AUTOTAG_HASH=$(git ls-remote origin "refs/tags/$AUTOTAG_TAG" | tail -1 | cut -f1)
    if [ -n "$AUTOTAG_HASH" ]; then
      echo "Tag $AUTOTAG_TAG exists already as $AUTOTAG_HASH, using it"
    elif [ "$DO_NOT_CREATE_NEW_TAG" = true ]; then
      # Tag does not exist, but we have requested this job to forcibly use an existing one.
      # Will abort the job.
      echo "Tag $AUTOTAG_TAG was not found, however we have been requested to not create a new one" \
          "(DO_NOT_CREATE_NEW_TAG is true). Aborting with error"
      exit 1
    else
      # Tag does not exist. Create release candidate branch, if not existing.
      AUTOTAG_HASH=$(git ls-remote origin "refs/heads/rc/$AUTOTAG_TAG" | tail -1 | cut -f1)

      if [ -n "$AUTOTAG_HASH" ] && [ "$REMOVE_RC_BRANCH_FIRST" = true ]; then
        # Remove branch first if requested. Error is fatal.
        git push origin ":refs/heads/rc/$AUTOTAG_TAG"
        AUTOTAG_HASH=
      fi

      if [ -z "$AUTOTAG_HASH" ]; then
        # Let's point it to HEAD
        AUTOTAG_HASH=$(git ls-remote origin HEAD | tail -1 | cut -f1)
        if [ -z "$AUTOTAG_HASH" ]; then
          echo "FATAL: Cannot find any hash pointing to HEAD (repo's default branch)!" >&2
          exit 1
        fi
        echo "Head of $AUTOTAG_REMOTE will be used, it's at $AUTOTAG_HASH"
      fi
    fi

    # At this point, we have $AUTOTAG_HASH for sure. It might come from HEAD, an existing rc/* branch,
    # or an existing tag. We always create a new branch out of it
    git push origin "+$AUTOTAG_HASH:refs/heads/rc/$AUTOTAG_TAG"
  fi
); done

: ${DEFAULTS:=release}

edit_tags () {
  # Patch package definition (e.g. o2.sh)
  local package tag=$1 version=$AUTOTAG_OVERRIDE_VERSION
  for package in $PACKAGES; do
    sed -E -i.old \
        "s|^tag: .*\$|tag: \"$tag\"|; ${version:+s|^version: .*\$|version: \"$version\"|}" \
        "alidist/${package,,}.sh"

    # Patch defaults definition (e.g. defaults-o2.sh)
    # Process overrides by changing in-place the given defaults. This requires
    # some YAML processing so we are better off with Python.
    tag=$tag package=$package DEFAULTS=$DEFAULTS $PYTHON <<\EOF
import yaml
from os import environ
f = "alidist/defaults-%s.sh" % environ["DEFAULTS"].lower()
p = environ["package"]
meta, rest = open(f).read().split("\n---\n", 1)
d = yaml.safe_load(meta)
open(f+".old", "w").write(yaml.dump(d)+"\n---\n"+rest)
write = False
overrides = d.setdefault("overrides", {}).setdefault(p, {})
if "tag" in overrides:
    overrides["tag"] = environ["tag"]
    write = True
v = environ.get("AUTOTAG_OVERRIDE_VERSION")
if v and "version" in overrides:
    overrides["version"] = v
    write = True
if write:
    open(f, "w").write(yaml.dump(d)+"\n---\n"+rest)
EOF
  done
}

# Switch the recipes for the packages specified in ALIDIST_OVERRIDE_PKGS
# to the version found in the alidist branch specified by ALIDIST_OVERRIDE_BRANCH
if [ ! "X$ALIDIST_OVERRIDE_BRANCH" = X ]; then
  git -C alidist checkout $ALIDIST_OVERRIDE_BRANCH -- $(echo $ALIDIST_OVERRIDE_PKGS |tr ",[:upper:]" "\ [:lower:]" | xargs -r -n1 echo | sed -e 's/$/.sh/g')
fi

# The tag doesn't exist yet, so build using the branch first.
edit_tags "rc/$AUTOTAG_TAG"

git -C alidist diff || :

# Select build directory in order to prevent conflicts and allow for cleanups.
workarea=$(mktemp -d "$PWD/daily-tags.XXXXXXXXXX")

# Set default remote store -- S3 on slc8 and Ubuntu, rsync everywhere else.
case "$ARCHITECTURE" in
  slc8_*|ubuntu*) : "${REMOTE_STORE:=b3://alibuild-repo::rw}" ;;
  *) : "${REMOTE_STORE:=rsync://repo.marathon.mesos/store/::rw}" ;;
esac
case "$REMOTE_STORE" in
  b3://*)
    set +x  # avoid leaking secrets
    . /secrets/aws_bot_secrets
    export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
    set -x ;;
esac
aliBuild --reference-sources mirror                    \
         --debug                                       \
         --work-dir "$workarea"                        \
         ${ARCHITECTURE:+--architecture $ARCHITECTURE} \
         --jobs "${JOBS:-8}"                           \
         --fetch-repos                                 \
         --remote-store $REMOTE_STORE                  \
         ${DEFAULTS:+--defaults $DEFAULTS}             \
         build "$main_pkg" || {
  builderr=$?
  echo "Exiting with an error ($builderr), not tagging"
  exit $builderr
}
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

# Now we tag, in case we should
for package in $PACKAGES; do
  pushd "${package,,}.git" &> /dev/null || continue
  if [ -n "$(git ls-remote origin "refs/tags/$AUTOTAG_TAG")" ]; then
    echo "Not tagging: tag $AUTOTAG_TAG exists already"
  else
    # We previously pushed AUTOTAG_HASH to refs/heads/rc/$AUTOTAG_TAG.
    # For some reason, +refs/remotes/origin/rc/$AUTOTAG_TAG:... doesn't work --
    # even though we push above, the ref doesn't seem to be remembered locally.
    autotag_hash=$(git ls-remote origin "refs/heads/rc/$AUTOTAG_TAG" | cut -f1)
    [ -n "$autotag_hash" ]
    git push origin "+$autotag_hash:refs/tags/$AUTOTAG_TAG"
  fi
  git push origin ":refs/heads/rc/$AUTOTAG_TAG" || :  # error is not a big deal here
  popd &> /dev/null
done

# Also tag the appropriate alidist
# We normally want to build using the tag, and now it exists.
edit_tags "$AUTOTAG_TAG"
cd alidist
edited_files=("defaults-${DEFAULTS,,}.sh")
for edited_pkg in $PACKAGES; do
  edited_files+=("${edited_pkg,,}.sh")
done
# If the file was modified, the output of git status will be non-empty.
if [ -n "$(git status --porcelain "${edited_files[@]}")" ]; then
  git add "${edited_files[@]}"
  git commit -m "Auto-update: ${edited_files[*]}"
fi
git push origin -f "HEAD:refs/tags/$main_pkg-$AUTOTAG_TAG"
# If ALIDIST_BRANCH doesn't exist or we can push to it, do it.
git push origin "HEAD:${ALIDIST_BRANCH:?}" ||
  # Else, make a PR by pushing an rc/ branch. (An action in the repo handles this.)
  git push origin -f "HEAD:rc/${ALIDIST_BRANCH:?}"
