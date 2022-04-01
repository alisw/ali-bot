#!/bin/bash -ex
set -exo pipefail

push_package_tag () {
  ## Create and push an rcbranch ($3) for the given package ($1), reusing the
  ## given tag ($2) if it exists.
  local package=$1 tag=$2 rcbranch=$3 remote mirror_repo commit_hash
  rm -rf "${package,,}.git"
  mirror_repo=$MIRROR/${package,,}
  [ -d "$mirror_repo" ] || mirror_repo=

  if remote=$(grep -E '^(source:|write_repo:)' "alidist/${package,,}.sh" |
                sort -r | head -n1 | cut -d: -f2- | xargs echo) &&
      [ -n "$remote" ]
  then
    echo "A Git tag will be created, upon success and if not existing, with the name $tag"
    echo "A Git branch will be created to pinpoint the build operation, with the name $rcbranch"
    mkdir "${package,,}.git"
    pushd "${package,,}.git"
    git clone --bare ${mirror_repo:+--reference=$mirror_repo} "$remote" .
    commit_hash=$(git ls-remote origin "refs/tags/$tag" | cut -f1)
    if [ -n "$commit_hash" ]; then
      echo "Tag $tag exists already as $commit_hash, using it"
    elif [ "$DO_NOT_CREATE_NEW_TAG" = true ]; then
      # Tag does not exist, but we have requested this job to forcibly use an existing one.
      # Will abort the job.
      echo "Tag $tag was not found, however we have been requested to not create a new one" \
           "(DO_NOT_CREATE_NEW_TAG is true). Aborting with error"
      exit 1
    else
      # Tag does not exist. Create release candidate branch, if not existing.
      commit_hash=$(git ls-remote origin "refs/heads/$rcbranch" | cut -f1)

      if [ -n "$commit_hash" ] && [ "$REMOVE_RC_BRANCH_FIRST" = true ]; then
        # Remove branch first if requested. Error is fatal.
        git push origin ":refs/heads/$rcbranch"
        commit_hash=
      fi

      if [ -z "$commit_hash" ]; then
        # Let's point it to HEAD
        commit_hash=$(git ls-remote origin HEAD | cut -f1)
        if [ -z "$commit_hash" ]; then
          echo "FATAL: Cannot find any hash pointing to HEAD (repo's default branch)!" >&2
          exit 1
        fi
        echo "Head of $remote will be used, it's at $commit_hash"
      fi
    fi

    # At this point, we have $commit_hash for sure. It might come from HEAD, an existing rc/* branch,
    # or an existing tag. We always create a new branch out of it
    git push -f origin "$commit_hash:refs/heads/$rcbranch"
    popd
  fi
}

edit_package_tag () {
  ## Change the tag and version of the given package ($2) to the given values
  ## (tag=$3, version=$4). If the given defaults ($1) override the tag or
  ## version, change those overrides to the given values as well.
  local defaults=$1 package=$2 tag=$3 version=$4

  # Patch package definition (e.g. o2.sh)
  sed -E -i.old \
      "s|^tag: .*\$|tag: \"$tag\"|; ${version:+s|^version: .*\$|version: \"$version\"|}" \
      "alidist/${package,,}.sh"

  # Patch defaults definition (e.g. defaults-o2.sh)
  # Process overrides by changing in-place the given defaults. This requires
  # some YAML processing so we are better off with Python.
  tag=$tag package=$package defaults=$defaults $python <<\EOF
import yaml
from os import environ
f = "alidist/defaults-%s.sh" % environ["defaults"].lower()
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
}

get_effective_package_tag () {
  ## Get the tag of the package $1 that aliBuild will use when building with
  ## defaults $2.
  : "${1:?}" "${2:?}"
  $python << EOF
import yaml
meta, _ = open("alidist/defaults-$2.sh".lower()).read().split("\n---\n", 1)
tag = yaml.safe_load(meta).get("overrides", {}).get("$1", {}).get("tag")
if not tag:
    meta, _ = open("alidist/$1.sh".lower()).read().split("\n---\n", 1)
    tag = yaml.safe_load(meta)["tag"]
print(tag)
EOF
}

git config --global user.name 'ALICE Builder'
git config --global user.email alibuild@cern.ch

: "${ALIDIST_SLUG:=alisw/alidist@master}" "${DEFAULTS:=release}"
if echo "$ALIDIST_SLUG" "$AUTOTAG_TAG" | grep -q '!!FLPSUITE_[A-Z]*!!'; then
  yum install -y jq

  # Sort available tags by version number, then pick the latest one.
  flpsuite_latest=$(curl -fSsLk https://ali-flp.cern.ch/tags | jq -r '[.[] | .name] | max')
  # Get the tag currently installed on the FLPs.
  flpsuite_current=flp-suite-v$(curl -fSsLk https://ali-flp.cern.ch/suite_version)

  # Override the patch version number to .0, as that is how alidist branches
  # should be named.
  flpsuite_latest=${flpsuite_latest%.*}.0
  flpsuite_current=${flpsuite_current%.*}.0

  # Monday (for Sunday night build)
  if [ "$(date +%u)" = 1 ]; then
    flpsuite_current=$flpsuite_latest
  fi

  ALIDIST_SLUG=${ALIDIST_SLUG//!!FLPSUITE_LATEST!!/$flpsuite_latest}
  AUTOTAG_TAG=${AUTOTAG_TAG//!!FLPSUITE_LATEST!!/$flpsuite_latest}
  ALIDIST_SLUG=${ALIDIST_SLUG//!!FLPSUITE_CURRENT!!/$flpsuite_current}
  AUTOTAG_TAG=${AUTOTAG_TAG//!!FLPSUITE_CURRENT!!/$flpsuite_current}
fi

# Determine branch from slug string: group/repo@ref
alidist_branch=${ALIDIST_SLUG##*@}
alidist_repo=${ALIDIST_SLUG%@*}
# PACKAGES contains whitespace-separated package names to tag. Only the first is
# built, but every listed package's tag is edited in the resulting commit. This
# enables tagging e.g. O2 and O2Physics at the same time, with the same tag, and
# building O2Physics (which pulls in O2 as well).
main_pkg=${PACKAGES%% *}
# Check for required variables
: "${PACKAGES:?}" "${main_pkg:?}" "${AUTOTAG_TAG:?}" "${NODE_NAME:?}"

if [ "$TEST_TAG" = true ]; then
  AUTOTAG_TAG=TEST-IGNORE-$AUTOTAG_TAG
fi

# Set the default python and pip depending on the architecture...
case "$NODE_NAME" in
  *slc8*) pip=pip3 python=python3 ;;
  *) pip=pip python=python ;;
esac
# ...and override it if PYTHON_VERSION is specified.
case "$PYTHON_VERSION" in
  2) pip=pip2 python=python2 ;;
  3) pip=pip3 python=python3 ;;
esac

# Install the latest release if ALIBUILD_SLUG is not provided
$pip install --user --upgrade "${ALIBUILD_SLUG:+git+https://github.com/}${ALIBUILD_SLUG:-alibuild}"

rm -rf alidist
git clone -b "$alidist_branch" "https://github.com/$alidist_repo" alidist

# Switch the recipes for the packages specified in ALIDIST_OVERRIDE_PKGS
# to the version found in the alidist branch specified by ALIDIST_OVERRIDE_BRANCH
if [ -n "$ALIDIST_OVERRIDE_BRANCH" ] && [ -n "$ALIDIST_OVERRIDE_PKGS" ]; then
  git -C alidist checkout "$ALIDIST_OVERRIDE_BRANCH" -- \
      $(echo "${ALIDIST_OVERRIDE_PKGS,,}" | xargs -rn 1 printf '%s.sh ')
fi

# Apply explicit tag overrides after possibly checking out the recipe from
# ALIDIST_OVERRIDE_BRANCH to allow combining the two effects.
for tagspec in $OVERRIDE_TAGS; do
  tag=${tagspec#*=}
  tag=${tag//!!FLPSUITE_LATEST!!/$flpsuite_latest}
  tag=${tag//!!FLPSUITE_CURRENT!!/$flpsuite_current}
  edit_package_tag "$DEFAULTS" "${tagspec%%=*}" "$tag"
done

# Now that we've overridden everything we are going to, see which tag we'll be
# using for each !!XYZ_TAG!! placeholder, and replace it in AUTOTAG_TAG. We need
# to do this before editing the recipes for $PACKAGES, as placeholders must be
# filled in then! As a result, we can't use !!PKG_TAG!! with those packages.
for tag_placeholder in $(echo "$AUTOTAG_TAG" | grep -o '!![^!]\+_TAG!!'); do
  package=${tag_placeholder#!!}
  package=${package%_TAG!!}
  case "${PACKAGES^^}" in
    "$package "*|*" $package "*|*" $package")
      echo "ERROR: can't use $tag_placeholder because $package is in PACKAGES array" >&2
      exit 1 ;;
  esac
  tag=$(get_effective_package_tag "$package" "$DEFAULTS")
  AUTOTAG_TAG=${AUTOTAG_TAG//$tag_placeholder/$tag}
done

if echo "$AUTOTAG_TAG" | grep -qF '!!'; then
  echo "ERROR: unrecognised placeholder remaining in AUTOTAG_TAG=$AUTOTAG_TAG" >&2
  exit 1
fi

# The tag doesn't exist yet, so build using the branch first. It's easiest to
# define priorities so that $PACKAGES takes priority over $OVERRIDE_TAGS, so
# that we don't need to edit stuff again for $OVERRIDE_TAGS after building.
for package in $PACKAGES; do
  edit_package_tag "$DEFAULTS" "$package" "rc/$AUTOTAG_TAG" "$AUTOTAG_OVERRIDE_VERSION"
done

git -C alidist diff || :

# Now that we have our final AUTOTAG_TAG value, create and push rc/$AUTOTAG_TAG
# branches for each package to be built.
for package in $PACKAGES; do
  push_package_tag "$package" "$AUTOTAG_TAG" "rc/$AUTOTAG_TAG"
done

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
    # We previously pushed $commit_hash to refs/heads/rc/$AUTOTAG_TAG.
    # For some reason, +refs/remotes/origin/rc/$AUTOTAG_TAG:... doesn't work --
    # even though we push above, the ref doesn't seem to be remembered locally.
    autotag_hash=$(git ls-remote origin "refs/heads/rc/$AUTOTAG_TAG" | cut -f1)
    git push origin "+${autotag_hash:?}:refs/tags/$AUTOTAG_TAG"
  fi
  git push origin ":refs/heads/rc/$AUTOTAG_TAG" || :  # error is not a big deal here
  popd &> /dev/null
done

# Also tag the appropriate alidist
# We normally want to build using the tag, and now it exists.
for package in $PACKAGES; do
  edit_package_tag "$DEFAULTS" "$package" "$AUTOTAG_TAG" "$AUTOTAG_OVERRIDE_VERSION"
done

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
# If $alidist_branch doesn't exist or we can push to it, do it.
git push origin "HEAD:${alidist_branch:?}" ||
  # Else, make a PR by pushing an rc/ branch. (An action in the repo handles
  # this.) Make sure we're pushing to a branch -- if $alidist_branch is a tag
  # name, this would fail otherwise.
  git push origin -f "HEAD:refs/heads/rc/${alidist_branch:?}"
