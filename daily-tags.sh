#!/bin/bash -ex
# -*- sh-basic-offset: 2 -*-

# Check for required variables
: "${PACKAGE_NAME:?}" "${AUTOTAG_PATTERN:?}" "${NODE_NAME:?}"
# Assign default values
: "${ALIDIST_SLUG:=alisw/alidist@master}" "${DEFAULTS:=release}"

# Clean up old stuff
rm -rf alidist/

# Determine branch from slug string: group/repo@ref
git clone -b "${ALIDIST_SLUG##*@}" "https://github.com/${ALIDIST_SLUG%@*}" alidist

# Install the latest release if ALIBUILD_SLUG is not provided
pip install --user --ignore-installed --upgrade "git+https://github.com/$ALIBUILD_SLUG"

AUTOTAG_REMOTE=$(grep -E '^(source:|write_repo:)' "alidist/${PACKAGE_NAME,,}.sh" |
                   sort -r | head -1 | cut -d' ' -f2-)
AUTOTAG_MIRROR=$MIRROR/${PACKAGE_NAME,,}
[ -d "$AUTOTAG_MIRROR" ] || AUTOTAG_MIRROR=
AUTOTAG_TAG=${TEST_TAG:+TEST-IGNORE-}$(LANG=C TZ=Europe/Zurich date "+$AUTOTAG_PATTERN")
echo "A Git tag will be created, upon success and if not existing, with the name $AUTOTAG_TAG"
AUTOTAG_BRANCH=rc/$AUTOTAG_TAG
echo "A Git branch will be created to pinpoint the build operation, with the name $AUTOTAG_BRANCH"
AUTOTAG_CLONE=$PWD/${PACKAGE_NAME,,}.git

if ! [ -e ../git-creds ]; then
  git config --global credential.helper "store --file ~/git-creds-autotag"  # backwards compat
fi

rm -rf "$AUTOTAG_CLONE"
git clone --bare "${AUTOTAG_MIRROR:+--reference=$AUTOTAG_MIRROR}" "$AUTOTAG_REMOTE" "$AUTOTAG_CLONE"
pushd "$AUTOTAG_CLONE" &>/dev/null
if git show-ref -q --verify "refs/tags/$AUTOTAG_TAG"; then
  autotag_hash=$(git show-ref -s "refs/tags/$AUTOTAG_TAG")
  echo "Tag $AUTOTAG_TAG exists already as $autotag_hash, using it"
elif [ -n "$DO_NOT_CREATE_NEW_TAG" ]; then
  # Tag does not exist, but we have requested this job to forcibly use an
  # existing one. Will abort the job.
  echo "Tag $AUTOTAG_TAG was not found, however we have been requested to not create a new one" \
       "(DO_NOT_CREATE_NEW_TAG is set). Aborting with error"
  exit 1
else
  # Tag does not exist. Create release candidate branch, if not existing.

  if git show-ref -q --verify "refs/heads/$AUTOTAG_BRANCH" && [ -n "$REMOVE_RC_BRANCH_FIRST" ]; then
    # Remove branch first if requested. Error is fatal.
    git push origin ":refs/heads/$AUTOTAG_BRANCH"
    git branch -D "$AUTOTAG_BRANCH"
  fi

  if git show-ref -q --verify "refs/heads/$AUTOTAG_BRANCH"; then
    autotag_hash=$(git show-ref -s "refs/heads/$AUTOTAG_BRANCH")
  else
    # Let's point it to HEAD
    if ! git show-ref -q --verify HEAD; then
      echo "FATAL: Cannot find any hash pointing to HEAD (repo's default branch)!" >&2
      exit 1
    fi
    autotag_hash=$(git show-ref -s HEAD)
    echo "Head of $AUTOTAG_REMOTE will be used, it's at $autotag_hash"
  fi
fi

# At this point, we have $autotag_hash for sure. It might come from HEAD, an
# existing rc/* branch, or an existing tag. We always create a new branch.
git push origin "+$autotag_hash:refs/heads/$AUTOTAG_BRANCH"

popd &>/dev/null  # exit Git repo

# Process overrides by changing in-place the given defaults. This requires some
# YAML processing so we are better off with Python.
python - "$AUTOTAG_BRANCH" "$PACKAGE_NAME" "alidist/defaults-${DEFAULTS,,}.sh" << EOF
import os, sys, yaml
_, branch, p, f = sys.argv
d = yaml.safe_load(open(f).read().split("---")[0])
open(f+".old", "w").write(yaml.dump(d)+"\n---\n")
d["overrides"] = d.get("overrides", {})
d["overrides"][p] = d["overrides"].get(p, {})
d["overrides"][p]["tag"] = branch
v = os.environ.get("AUTOTAG_OVERRIDE_VERSION")
if v: d["overrides"][p]["version"] = v
open(f, "w").write(yaml.dump(d)+"\n---\n")
EOF

diff -upN "alidist/defaults-${DEFAULTS,,}.sh"{.old,} | cat

# Select build directory in order to prevent conflicts and allow for cleanups.
workarea=$(mktemp -dp "$PWD" daily-tags.XXXXXXXXXX)

echo "Now running aliBuild on $JOBS parallel workers"
aliBuild --reference-sources mirror --work-dir "$workarea"        \
         ${ARCHITECTURE:+--architecture "$ARCHITECTURE"}          \
         --remote-store "${REMOTE_STORE:-s3://alibuild-repo::rw}" \
         --defaults "$DEFAULTS" --fetch-repos --jobs 8 --debug    \
         build "$PACKAGE_NAME" || {
  builderr=$?
  echo "Exiting with an error ($builderr), not tagging"
  exit "$builderr"
}

(
  # Now we tag
  cd "$AUTOTAG_CLONE"
  git push origin "+$autotag_hash:refs/tags/$AUTOTAG_TAG"
  # Delete the branch we created earlier.
  git push origin ":refs/heads/$AUTOTAG_BRANCH" || true  # error is not a big deal here
)

# Also tag the appropriate alidist
(cd alidist && git push origin "HEAD:refs/tags/$PACKAGE-$AUTOTAG_TAG")

rm -rf "$workarea" alidist
