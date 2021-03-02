#!/bin/bash -ex

edit_tags () {
  # Patch package definition (e.g. o2.sh)
  local tag=$1 version=$AUTOTAG_OVERRIDE_VERSION
  sed -ri.old "s|^tag: .*\$|tag: \"$tag\"|; ${version:+s|^version: .*\$|version: \"$version\"|}" \
      "$alidist_clone/${PACKAGE_NAME,,}.sh"

  # Patch defaults definition (e.g. defaults-o2.sh)
  # Process overrides by changing in-place the given defaults. This requires
  # some YAML processing so we are better off with Python.
  tag=$1 pkg=$PACKAGE_NAME def=${DEFAULTS,,} dist=$alidist_clone python << EOF
import yaml
from os import environ
f = "%(dist)s/defaults-%(def)s.sh" % environ
p = environ["pkg"]
meta, rest = open(f).read().split("\n---\n", 1)
d = yaml.safe_load(meta)
open(f+".old", "w").write(yaml.dump(d)+"\n---\n"+rest)
d["overrides"] = d.get("overrides", {})
d["overrides"][p] = d["overrides"].get(p, {})
d["overrides"][p]["tag"] = environ["tag"]
v = environ.get("AUTOTAG_OVERRIDE_VERSION")
if v:
    d["overrides"][p]["version"] = v
open(f, "w").write(yaml.dump(d)+"\n---\n"+rest)
EOF
}

# Check for required variables
: "${PACKAGE_NAME:?}" "${AUTOTAG_TAG:?}"
: "${DEFAULTS:=release}" "${ALIDIST_SLUG:=alisw/alidist@master}"

# Determine branch from slug string: group/repo@ref
alidist_branch=${ALIDIST_SLUG##*@}
autotag_branch=rc/$AUTOTAG_TAG
autotag_clone=$PWD/${PACKAGE_NAME,,}.git
echo "A Git tag will be created, upon success and if not existing, with the name $AUTOTAG_TAG" >&2
echo "A Git branch will be created to pinpoint the build operation, with the name $autotag_branch" >&2
alidist_clone=$PWD/alidist

# Select build directory in order to prevent conflicts and allow for cleanups.
workarea=$(mktemp -d "$PWD/daily-tags.XXXXXXXXXX")

# Even when exiting with an error, clean up build area.
trap 'rm -rf "$alidist_clone" "$autotag_clone" "$workarea"' EXIT

rm -rf "$alidist_clone" "$autotag_clone"
git clone -b "$alidist_branch" "https://github.com/${ALIDIST_SLUG%@*}" "$alidist_clone"
git clone --bare "$(grep -E '^(source:|write_repo:)' "$alidist_clone/${PACKAGE_NAME,,}.sh" |
                      sort -r | head -1 | cut -d: -f2- | xargs echo)" "$autotag_clone"

pushd "$autotag_clone" &>/dev/null
autotag_origin=

if autotag_hash=$(git rev-parse --verify --end-of-options "$AUTOTAG_TAG"); then
  echo "Tag $AUTOTAG_TAG exists already as $autotag_hash, using it" >&2
  autotag_origin=tag
elif [ "$DO_NOT_CREATE_NEW_TAG" = true ]; then
  # Tag does not exist, but we have requested this job to forcibly use an
  # existing one. Will abort the job.
  echo "Tag $AUTOTAG_TAG was not found, however we have been requested" \
       "to not create a new one (DO_NOT_CREATE_NEW_TAG is true)."       \
       "Aborting with error" >&2
  exit 1
elif autotag_hash=$(git rev-parse --verify HEAD); then
  # Tag does not exist. Let's point it to HEAD.
  echo "Head of $(git remote get-url origin) will be used, it's at $autotag_hash" >&2
else
  echo "FATAL: Cannot find any hash pointing to HEAD (repo's default branch)!" >&2
  exit 1
fi

if [ "$REMOVE_RC_BRANCH_FIRST" = true ] && [ "$autotag_origin" != tag ] &&
     # Make sure branch exists before we remove it, so we don't get an error.
     git rev-parse --verify --end-of-options "$autotag_branch"
then
  # Remove existing branch first if requested. Only necessary if we aren't using
  # the existing tag. Error is fatal.
  git push origin ":refs/heads/$autotag_branch"
fi

# At this point, we have $autotag_hash for sure. It might come from HEAD, an
# existing rc/* branch, or an existing tag. We always create a new branch.
git push origin "+$autotag_hash:refs/heads/$autotag_branch"
popd &>/dev/null

# The tag doesn't exist yet, so build using the branch first.
edit_tags "$autotag_branch"

# diff(1) exits with 1 if the inputs are different. This is expected; it
# shouldn't fail the build!
diff -Nup "$alidist_clone/defaults-${DEFAULTS,,}.sh"{.old,} || true

aliBuild build "$PACKAGE_NAME" --debug --fetch-repos --jobs 10 --work-dir "$workarea" \
         --defaults "$DEFAULTS" ${ARCHITECTURE:+--architecture "$ARCHITECTURE"}       \
         --remote-store "${REMOTE_STORE:-rsync://repo.marathon.mesos/store/::rw}"  || {
  builderr=$?
  echo "Exiting with an error ($builderr), not tagging" >&2
  exit $builderr
}

# Now we tag the package, in case we should.
cd "$autotag_clone"
if [ "$autotag_origin" = tag ]; then
  echo "Not tagging: tag $AUTOTAG_TAG exists already as $autotag_hash" >&2
else
  git push origin "+$autotag_hash:refs/tags/$AUTOTAG_TAG"
fi
git push origin ":refs/heads/$autotag_branch" || true  # error is not a big deal here

# Also tag the appropriate alidist.
# We normally want to build using the tag, and now it exists.
edit_tags "$AUTOTAG_TAG"
cd "$alidist_clone"
defaults_fname=defaults-${DEFAULTS,,}.sh pkg_fname=${PACKAGE_NAME,,}.sh
# If the file was modified, the output of git status will be non-empty.
if [ -n "$(git status --porcelain "$defaults_fname" "$pkg_fname")" ]; then
  git add "$defaults_fname" "$pkg_fname"
  git commit -m "Auto-update $defaults_fname and $pkg_fname"
fi
git push origin -f "HEAD:refs/tags/$PACKAGE_NAME-$AUTOTAG_TAG"
# If alidist_branch doesn't exist or we can push to it, do it.
git push origin "HEAD:$alidist_branch" ||
  # Else, make a PR by pushing an rc/ branch. (An action in the repo handles this.)
  git push origin -f "HEAD:rc/$alidist_branch"
