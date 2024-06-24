#!/bin/bash -ex
set -ex

substitute_vars () {
  local value=$1
  value=${value//!!FLPSUITE_LATEST!!/$flpsuite_latest}
  value=${value//!!FLPSUITE_CURRENT!!/$flpsuite_current}
  value=${value//!!ALIDIST_BRANCH!!/$ALIDIST_BRANCH}
  LANG=C TZ=Europe/Zurich date -d "@$START_TIMESTAMP" "+$value"
}

edit_package_tag () {
  # Patch package definition (e.g. o2.sh) with a new tag and version, changing
  # the defaults as well if necessary.
  local package=${1:?edit_package_tag: package not provided} defaults=${2:?edit_package_tag: defaults not provided} tag=$3 version=$4
  [ -n "$tag$version" ] || return 0

  sed -E -i "${tag:+"s|^tag: .*$|tag: \"$tag\"|;"} ${version:+"s|^version: .*\$|version: \"$version\"|"}" "alidist/${package,,}.sh"

  # Patch defaults definition (e.g. defaults-o2.sh)
  # Process overrides by changing in-place the given defaults. This requires
  # some YAML processing so we are better off with Python.
  package=$package defaults=$defaults tag=$tag version=$version python3 <<\EOF
import os, yaml
f = "alidist/defaults-%s.sh" % os.environ["defaults"].lower()
with open(f, "r") as recipef:
    meta, sep, rest = recipef.read().partition("\n---\n")
assert sep, "could not parse defaults recipe in %s" % f
d = yaml.safe_load(meta)
write = False
overrides = d.setdefault("overrides", {}).setdefault(os.environ["package"], {})
for key in ("tag", "version"):
    if key in os.environ and key in overrides:
        overrides[key] = os.environ[key]
        write = True
if write:
    with open(f, "w") as outf:
        yaml.dump(d, outf); outf.write(sep); outf.write(rest)
EOF
}

# PACKAGES contains whitespace-separated package names to tag. Only the first is
# built, but every listed package's tag is edited in the resulting commit. This
# enables tagging e.g. O2 and O2Physics at the same time, with the same tag, and
# building O2Physics (which pulls in O2 as well).
main_pkg=${PACKAGES%% *}
# Check for required variables
: "${PACKAGES:?}" "${main_pkg:?}" "${NODE_NAME:?}"
: "${ALIDIST_SLUG:=alisw/alidist@master}" "${DEFAULTS:=release}"

# Determine branch from slug string: group/repo@ref
ALIDIST_BRANCH="${ALIDIST_SLUG##*@}"
ALIDIST_REPO="${ALIDIST_SLUG%@*}"

# Clean up old stuff
rm -rf alidist/

git config --global user.name 'ALICE Builder'
git config --global user.email alibuild@cern.ch

# Upgrade pip
python3 -m pip install --user --upgrade pip
# Install the latest release if ALIBUILD_SLUG is not provided
python3 -m pip install --user --upgrade "${ALIBUILD_SLUG:+git+https://github.com/}${ALIBUILD_SLUG:-alibuild}"
aliBuild analytics off

# The alidist branches are always named with a trailing .0 instead of the
# "normal" patch release number.
flpsuite_latest=$(git ls-remote "https://github.com/$ALIDIST_REPO" -- 'refs/heads/flp-suite-v*' |
                    cut -f2 | sort -V | sed -rn '$s,^refs/heads/(.*)\.[0-9]+$,\1.0,p')
# In case ali-flp is offline, don't fail if we don't need its info.
if echo "$ALIDIST_BRANCH $AUTOTAG_PATTERN $OVERRIDE_TAGS" | grep -qi 'flpsuite_current'; then
  flpsuite_current=flp-suite-v$(curl -fSsLk https://ali-flp.cern.ch/suite_version)
  flpsuite_current=${flpsuite_current%.*}.0
fi
if [ "$(date +%u)" = 1 ]; then   # On Mondays (for Sunday night builds)
  flpsuite_current=$flpsuite_latest
fi

ALIDIST_BRANCH=${ALIDIST_BRANCH//!!FLPSUITE_LATEST!!/$flpsuite_latest}
ALIDIST_BRANCH=${ALIDIST_BRANCH//!!FLPSUITE_CURRENT!!/$flpsuite_current}
if ! git clone -b "$ALIDIST_BRANCH" "https://github.com/$ALIDIST_REPO" alidist/; then
  # We may have been given a commit hash as $ALIDIST_BRANCH, and we can't pass
  # hashes to -b. Clone and checkout instead.
  git clone "https://github.com/$ALIDIST_REPO" alidist/
  (cd alidist && git checkout -f "$ALIDIST_BRANCH")
fi

# Switch the recipes for the packages specified in ALIDIST_OVERRIDE_PKGS
# to the version found in the alidist branch specified by ALIDIST_OVERRIDE_BRANCH
if [ -n "$ALIDIST_OVERRIDE_BRANCH" ]; then (
  cd alidist
  git checkout "$ALIDIST_OVERRIDE_BRANCH" -- \
      $(echo "$ALIDIST_OVERRIDE_PKGS" |
          tr ',[:upper:]' ' [:lower:]' |
          xargs -rn1 echo | sed 's/$/.sh/')
); fi

# Apply explicit tag overrides after possibly checking out the recipe from
# ALIDIST_OVERRIDE_BRANCH to allow combining the two effects.
for tagspec in $OVERRIDE_TAGS; do
  edit_package_tag "${tagspec%%=*}" "$DEFAULTS" "$(substitute_vars "${tagspec#*=}")" ''
done
for versionspec in $OVERRIDE_VERSIONS; do
  edit_package_tag "${versionspec%%=*}" "$DEFAULTS" '' "$(substitute_vars "${versionspec#*=}")"
done

# Select build directory in order to prevent conflicts and allow for cleanups.
workarea=$(mktemp -d "$PWD/daily-tags.XXXXXXXXXX")

# NOMAD_CPU_CORES is of the form "0-2,7,12-14". Get the total number of
# assigned cores from that (i.e. 7 cores in the example). For some Nomad jobs,
# we manually set MAX_CORES, so only assign to it if it's not already set.
: "${MAX_CORES:=$(echo "$NOMAD_CPU_CORES" | tr , '\n' | awk -F- '/^./{num_cores += $2 ? 1 + $2 - $1 : 1} END{print num_cores}')}"

# Define aliBuild args once, so that we have (mostly) the same args for
# templating and the real build.
alibuild_args=(
  --debug --work-dir "$workarea"
  # JOBS is set by the Jenkins job, MAX_CORES is set by Nomad.
  --jobs "${JOBS:-${MAX_CORES:-$(nproc)}}"
  --reference-sources mirror
  ${ARCHITECTURE:+--architecture "$ARCHITECTURE"}
  ${DEFAULTS:+--defaults "$DEFAULTS"}
  build "$main_pkg"
)

# Assignments are specified as a multi-line string with one VAR=value pair per line.
while read -r assignment; do
  if [ -n "$assignment" ]; then
    alibuild_args+=(-e "$assignment")
  fi
done <<< "$BUILD_ENV_VARS"

# Attach any user-specified comment to all top-level packages.
if [ -n "$BUILD_COMMENT" ] &&
     # Make sure that this aliBuild supports --annotate.
     aliBuild build --help | grep -q -- --annotate; then
  for package in $PACKAGES; do
    alibuild_args+=(--annotate "$package=$BUILD_COMMENT")
  done
fi

# Process the pattern as a jinja2 template with aliBuild's templating plugin.
# Fetch the source repos now, so they're available for the "real" build later.
AUTOTAG_PATTERN=$(aliBuild --plugin templating --fetch-repos "${alibuild_args[@]}" << EOF
{%- set alidist_branch = "$ALIDIST_BRANCH" -%}
{%- set flpsuite_latest = "$flpsuite_latest" -%}
{%- set flpsuite_current = "$flpsuite_current" -%}
$AUTOTAG_PATTERN
EOF
)

# Finally, replace strftime formatting (%Y, %m, %d etc) in the pattern.
AUTOTAG_TAG=$(LANG=C TZ=Europe/Zurich date -d "@$START_TIMESTAMP" "+$AUTOTAG_PATTERN")

: "${AUTOTAG_TAG:?}"   # make sure the tag isn't empty
[ "$TEST_TAG" = true ] && AUTOTAG_TAG=TEST-IGNORE-$AUTOTAG_TAG

if echo "$AUTOTAG_TAG" | grep -Fq '!!'; then
  echo "Tag $AUTOTAG_TAG contains !!-placeholders! These will not be replaced; use Jinja2 syntax instead. Exiting." >&2
  exit 1
fi

# The tag doesn't exist yet, so build using the branch first.
for package in $PACKAGES; do
  edit_package_tag "$package" "$DEFAULTS" "rc/$AUTOTAG_TAG" "$AUTOTAG_OVERRIDE_VERSION"
done

(cd alidist && git diff) || :

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

# Set default remote store -- S3 on slc8 and Ubuntu, rsync everywhere else.
case "$ARCHITECTURE" in
  slc[567]_x86-64) : "${REMOTE_STORE:=b3://alibuild-repo::rw}" ;;
  *) : "${REMOTE_STORE:=b3://alibuild-repo::rw}" ;;
esac
case "$REMOTE_STORE" in
  b3://*)
    set +x  # avoid leaking secrets
    . /secrets/aws_bot_secrets
    export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
    set -x ;;
esac
aliBuild --remote-store "$REMOTE_STORE" "${alibuild_args[@]}" || {
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
: "${PACKAGES_IN_ALIDIST_TAG:=$PACKAGES}"
# We normally want to build using the tag, and now it exists.
(cd alidist && git stash)   # first, undo our temporary changes, which might include changes that shouldn't be committed
for package in $PACKAGES_IN_ALIDIST_TAG; do
  edit_package_tag "$package" "$DEFAULTS" "$AUTOTAG_TAG" "$AUTOTAG_OVERRIDE_VERSION"
done
cd alidist
edited_files=("defaults-${DEFAULTS,,}.sh")
for edited_pkg in $PACKAGES_IN_ALIDIST_TAG; do
  edited_files+=("${edited_pkg,,}.sh")
done
# If the file was modified, the output of git status will be non-empty.
if [ -n "$(git status --porcelain "${edited_files[@]}")" ]; then
  git add "${edited_files[@]}"
  git commit -m "Auto-update: ${edited_files[*]}"
fi
git push origin -f "HEAD:refs/tags/$main_pkg-$AUTOTAG_TAG"

if [ "$CREATE_ALIDIST_PULL_REQUEST" = true ]; then
  # If ALIDIST_BRANCH doesn't exist or we can push to it, do it.
  git push origin "HEAD:${ALIDIST_BRANCH:?}" ||
    # Else, make a PR by pushing an rc/ branch. (An action in the repo handles this.)
    git push origin -f "HEAD:refs/heads/rc/${ALIDIST_BRANCH:?}"
fi
