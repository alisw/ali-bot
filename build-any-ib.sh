#!/bin/bash -ex

# Two ways of specifying alidist: <group>/<repo>[@<branch>], <group>/<repo>#<prnum>
ALIDIST_BRANCH= ALIDIST_REPO=
case "$ALIDIST_SLUG" in
  *@*)  ALIDIST_REPO=${ALIDIST_SLUG%%@*}
        ALIDIST_BRANCH=${ALIDIST_SLUG#*@} ;;
  *\#*) ALIDIST_REPO=${ALIDIST_SLUG%%#*}
        ALIDIST_PRNUM=${ALIDIST_SLUG#*#} ;;
  *)    ALIDIST_REPO=$ALIDIST_SLUG ;;
esac

hostname
echo $RIEMANN_HOST
which lsb_release > /dev/null 2>&1 && lsb_release -a
uname -a
date

BUILD_DATE=$(echo 2015$(echo "$(date -u +%s) / (86400 * 3)" | bc))

MIRROR=mirror

# Allow for $WORKAREA to be overridden so that we can build special
# builds (e.g. coverage ones) in a different PATH.
WORKAREA=${WORKAREA:-sw/$BUILD_DATE}
WORKAREA_INDEX=0

# Get aliBuild with pip in a temporary directory. Gets all dependencies too
export PYTHONUSERBASE=$(mktemp -d)
export PATH=$PYTHONUSERBASE/bin:$PATH
export LD_LIBRARY_PATH=$PYTHONUSERBASE/lib:$LD_LIBRARY_PATH
# Set the default python and pip depending on the architecture...
case $ARCHITECTURE in
  slc7*|slc8*) PIP=pip3 PYTHON=python3 ;;
  *) PIP=pip PYTHON=python ;;
esac
# ...and override it if PYTHON_VERSION is specified.
case "$PYTHON_VERSION" in
  2) PIP=pip2 PYTHON=python2 ;;
  3) PIP=pip3 PYTHON=python3 ;;
esac
$PIP install --user --upgrade "${ALIBUILD_SLUG:+git+https://github.com/}${ALIBUILD_SLUG:-alibuild}"
type aliBuild

rm -rf alidist
if [[ $ALIDIST_PRNUM ]]; then
  ALIDIST_BRANCH=pull/${ALIDIST_PRNUM}/head
  git clone "https://github.com/$ALIDIST_REPO" alidist
  pushd alidist
    alidist_local_branch=${ALIDIST_BRANCH//\//_}
    git fetch origin "$ALIDIST_BRANCH:$alidist_local_branch"
    git checkout "$alidist_local_branch"
  popd
else
  git clone ${ALIDIST_BRANCH:+-b "$ALIDIST_BRANCH"} \
      "https://github.com/$ALIDIST_REPO" alidist
fi

CURRENT_SLAVE=unknown
while [[ "$CURRENT_SLAVE" != '' ]]; do
  WORKAREA_INDEX=$((WORKAREA_INDEX+1))
  CURRENT_SLAVE=$(cat $WORKAREA/$WORKAREA_INDEX/current_slave 2> /dev/null || true)
  [[ "$CURRENT_SLAVE" == "$NODE_NAME" ]] && CURRENT_SLAVE=
done

mkdir -p $WORKAREA/$WORKAREA_INDEX
echo $NODE_NAME > $WORKAREA/$WORKAREA_INDEX/current_slave

# If no "defaults" is specified, default to "release"
: ${DEFAULTS:=release}

# Process overrides by changing in-place the given defaults. This requires some
# YAML processing so we are better off with Python.
env OVERRIDE_TAGS="$OVERRIDE_TAGS"         \
    OVERRIDE_VERSIONS="$OVERRIDE_VERSIONS" \
    DEFAULTS="$DEFAULTS"                   \
$PYTHON <<\EOF
import yaml
from os import environ
f = "alidist/defaults-%s.sh" % environ["DEFAULTS"].lower()
d = yaml.safe_load(open(f).read().split("---")[0])
open(f+".old", "w").write(yaml.dump(d)+"\n---\n")
d["overrides"] = d.get("overrides", {})
for t in environ.get("OVERRIDE_TAGS", "").split():
  p,t = t.split("=", 1)
  d["overrides"][p] = d["overrides"].get(p, {})
  d["overrides"][p]["tag"] = t
for v in environ.get("OVERRIDE_VERSIONS", "").split():
  p,v = v.split("=", 1)
  d["overrides"][p] = d["overrides"].get(p, {})
  d["overrides"][p]["version"] = v
open(f, "w").write(yaml.dump(d)+"\n---\n")
EOF

# List differences applied to the selected defaults
DEFAULTS_LOWER=$(echo $DEFAULTS | tr '[[:upper:]]' '[[:lower:]]')
diff -rupN alidist/defaults-${DEFAULTS_LOWER}.sh.old alidist/defaults-${DEFAULTS_LOWER}.sh || true

# Allow to specify AliRoot and AliPhysics as a development packages
if [[ "$ALIROOT_DEVEL_VERSION" != '' ]]; then
  aliBuild init AliRoot@$ALIROOT_DEVEL_VERSION
  pushd AliRoot
    # Either pull changes in the branch or reset it to the requested tag
    git pull origin || git reset --hard $ALIROOT_DEVEL_VERSION
  popd
else
  rm -rf AliRoot
fi
if [[ "$ALIPHYSICS_DEVEL_VERSION" != '' ]]; then
  aliBuild init AliPhysics@$ALIPHYSICS_DEVEL_VERSION
  pushd AliPhysics
    # Either pull changes in the branch or reset it to the requested tag
    git pull origin || git reset --hard $ALIPHYSICS_DEVEL_VERSION
  popd
else
  rm -rf AliPhysics
fi

: "${REMOTE_STORE:=b3://alibuild-repo}"
[ "$PUBLISH_BUILDS" = true ] && REMOTE_STORE=$REMOTE_STORE::rw
[ "$USE_REMOTE_STORE" = false ] && REMOTE_STORE=
case "$REMOTE_STORE" in
  b3://*)
    set +x  # avoid leaking secrets
    . /secrets/aws_bot_secrets
    export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
    set -x ;;
esac

FETCH_REPOS="$(aliBuild build --help | grep fetch-repos || true)"
aliBuild --reference-sources "$MIRROR"                    \
         --debug                                          \
         --work-dir "$WORKAREA/$WORKAREA_INDEX"           \
         ${FETCH_REPOS:+--fetch-repos}                    \
         --architecture "$ARCHITECTURE"                   \
         --jobs "${JOBS:-${MAX_CORES:-$(nproc)}}"         \
         ${REMOTE_STORE:+--remote-store "$REMOTE_STORE"}  \
         ${DEFAULTS:+--defaults "$DEFAULTS"}              \
         ${DISABLE:+--disable "$DISABLE"}                 \
         ${BUILD_COMMENT:+--annotate "$PACKAGE_NAME=$BUILD_COMMENT"} \
         build "$PACKAGE_NAME" || BUILDERR=$?

for logf in "$WORKAREA/$WORKAREA_INDEX/BUILD"/*/*/*.log; do
  [ -e "$logf" ] || continue
  s3cmd put "$logf" "s3://alibuild-repo/build-any-ib-logs/$logf" || :
done

rm -f $WORKAREA/$WORKAREA_INDEX/current_slave
[[ "$BUILDERR" != '' ]] && exit $BUILDERR

ALIDIST_HASH=$(cd $WORKSPACE/alidist && git rev-parse HEAD)
ALIBUILD_HASH=$(aliBuild version 2> /dev/null || true)
rm -rf $PYTHONUSERBASE
