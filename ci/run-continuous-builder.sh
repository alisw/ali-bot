#!/bin/bash -e

# Runs the continuous-builder.sh script as a standalone process.
#
# Usage: run-continuous-builder.sh <profile> [--test-build] [--test-doctor] [--list]
#
# <profile> refers to <path_to_this_script>/conf/<profile>.sh, containing a configuration in the
# form of shell variables (the script will be sourced).
#
#   --list         List returned PRs for the given configuration from <profile>
#   --test-doctor  Run aliDoctor with the configuration from <profile>
#   --test-build   Run aliBuild once with the configuration from <profile>
#
# Normal, non-interactive operations require no option.

PROGDIR=$(cd "$(dirname "$0")"; pwd)
cd "$PROGDIR"

PROG="$PROGDIR/$(basename "$0")"
CONF="$PROGDIR/conf/$1.sh"
[[ -r "$CONF" ]] || { echo "Cannot load profile \"$1\"! Valid options: $(ls "$(dirname "$0")"/conf | sed -e 's/\.sh$//' | xargs echo)"; exit 2; }
source "$CONF"
source ~/.continuous-builder || true
ERR=0
for V in GITHUB_TOKEN GITLAB_USER GITLAB_PASS PR_REPO PACKAGE CHECK_NAME PR_BRANCH ALIBUILD_DEFAULTS INFLUXDB_WRITE_URL AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
  [[ $(eval echo \$$V) ]] || { echo "Required variable $V not defined!"; ERR=1; continue; }
  eval "export $V"
done
if [[ $WORKERS_POOL_SIZE && ! $WORKER_INDEX ]]; then
  echo "When defining WORKERS_POOL_SIZE one should define WORKER_INDEX as well!"
  ERR=1
fi
[[ $ERR == 1 ]] && exit 1

# If we are in a virtual environment do not install with --user
if [[ $VIRTUAL_ENV ]]; then
  PIP_USER=
else
  PIP_USER='--user'
fi

JOBS_DEFAULT=$(sysctl -n hw.ncpu 2> /dev/null || grep -c bogomips /proc/cpuinfo 2> /dev/null || echo 4)

# We allow variables to be set externally, and to be set to empty strings as well.
# This is why we use '-' instead of ':-' in the default expansion.
export ALIBOT_ANALYTICS_ID="UA-77346950-2"
export WORKERS_POOL_SIZE=${WORKERS_POOL_SIZE-1}
export WORKER_INDEX=${WORKER_INDEX-0}
export REMOTE_STORE=${REMOTE_STORE-''}
export NO_ASSUME_CONSISTENT_EXTERNALS=${NO_ASSUME_CONSISTENT_EXTERNALS-true}
export BUILD_SUFFIX=${BUILD_SUFFIX-master}
export TRUSTED_USERS=${TRUSTED_USERS-ktf,dberzano}
export TRUST_COLLABORATORS=${TRUST_COLLABORATORS-true}
export PR_REPO_CHECKOUT=${PR_REPO_CHECKOUT:-$PACKAGE}
export JOBS=${JOBS-$JOBS_DEFAULT}
export DONT_USE_COMMENTS
export ALIBUILD_O2_TESTS=1
export ALIBUILD_REPO="alisw/alibuild"
export MONALISA_HOST=aliendb9.cern.ch
export MONALISA_PORT=8885
export MAX_DIFF_SIZE=20000000
export DELAY=20
export DEBUG=true
export MIRROR=/Users/alibuild/build/mirror

# Disable aliBuild analytics prompt
mkdir -p $HOME/.config/alibuild
touch $HOME/.config/alibuild/disable-analytics

[[ `uname` == Darwin ]] && OS=macos || OS=linux
export CI_NAME=$(echo $PR_REPO_CHECKOUT|tr '[[:upper:]]' '[[:lower:]]')_checker_${OS}_${ALIBUILD_DEFAULTS}_ci

# Setup working directory and local Python installation
ALIBOT="$(cd ..;pwd)"
CI_WORK_DIR=/Users/alibuild/build/ci_checks/${CI_NAME}_${WORKER_INDEX}
BREW_PATH=/usr/local/bin
ALIBOT_PATH=/Users/alibuild/build/ali-bot
export PYTHONUSERBASE="$CI_WORK_DIR/python_local"
export PATH="$PYTHONUSERBASE/bin:$BREW_PATH:$ALIBOT_PATH:$ALIBOT_PATH/analytics:$ALIBOT_PATH/ci:$PATH"
export LD_LIBRARY_PATH="$PYTHONUSERBASE/lib:$LD_LIBRARY_PATH"
mkdir -p "$CI_WORK_DIR" "$PYTHONUSERBASE" "$CI_WORK_DIR/logs"

# aliBuild repository slug: <group>/<repo>[@<branch>]
ALIBUILD_SLUG=${ALIBUILD_SLUG:-alisw/alibuild}
ALIBUILD_REPO=${ALIBUILD_SLUG%%@*}
ALIBUILD_BRANCH=${ALIBUILD_SLUG#*@}
[[ $ALIBUILD_REPO == $ALIBUILD_SLUG ]] && ALIBUILD_BRANCH= || true

# Install aliBuild through pip (ensures dependencies are installed as well)
pip install ${PIP_USER} --ignore-installed --upgrade git+https://github.com/${ALIBUILD_REPO}${ALIBUILD_BRANCH:+@$ALIBUILD_BRANCH}
type aliBuild

set -x
cd "$CI_WORK_DIR"

[[ -d alidist/.git ]]             || git clone https://github.com/alisw/alidist
[[ -d "$PR_REPO_CHECKOUT/.git" ]] || git clone "https://github.com/$PR_REPO" "$PR_REPO_CHECKOUT"

# Extra repositories to download are in array EXTRA_REPOS. Each element is in the form:
#   repo=alisw/alidist [branch=dev] [checkout=AliDist]
# where branch, checkout are optional.
for EXTRA in "${EXTRA_REPOS[@]}"; do
  unset repo branch checkout
  EXTRA_REPO=$(eval "$EXTRA"; echo $repo)
  EXTRA_BRANCH=$(eval "$EXTRA"; echo $branch)
  EXTRA_CHECKOUT=$(eval "$EXTRA"; echo $checkout)
  [[ -d "$EXTRA_CHECKOUT/.git" ]] || git clone https://github.com/$EXTRA_REPO ${EXTRA_BRANCH:+-b $EXTRA_BRANCH} ${EXTRA_CHECKOUT:+$EXTRA_CHECKOUT/}
done

if [[ $2 != --test* ]]; then
  ( cd alidist;           git fetch origin master; git checkout master; git reset --hard origin/master; )
  ( cd $PR_REPO_CHECKOUT; git fetch origin "$PR_BRANCH"; git checkout "$PR_BRANCH"; git reset --hard "origin/$PR_BRANCH"; )
else
  echo "Test mode: will not update repositories" >&2
fi

# Setup Git user under checked out repository
pushd $PR_REPO_CHECKOUT
  git config user.name alibuild
  git config user.email alibuild@cern.ch
popd

case "$2" in

  --list)
    set -x
    $ALIBOT/list-branch-pr --show-main-branch --check-name $CHECK_NAME ${TRUST_COLLABORATORS:+--trust-collaborators} ${TRUSTED_USERS:+--trusted $TRUSTED_USERS} $PR_REPO@$PR_BRANCH ${WORKERS_POOL_SIZE:+--workers-pool-size $WORKERS_POOL_SIZE} ${WORKER_INDEX:+--worker-index $WORKER_INDEX} ${DELAY:+--max-wait $DELAY}
    exit 0
  ;;

  --test-build)
    set -x
    PACKAGE=${3-$PACKAGE}
    aliBuild init $PACKAGE --defaults $ALIBUILD_DEFAULTS --reference-source $MIRROR
    exec aliBuild build $PACKAGE --defaults $ALIBUILD_DEFAULTS ${DEBUG:+--debug} --reference-source $MIRROR
  ;;

  --test-doctor)
    set -x
    PACKAGE=${3-$PACKAGE}
    exec aliDoctor $PACKAGE --defaults $ALIBUILD_DEFAULTS ${DEBUG:+--debug}
  ;;

  "")
  ;;

  *)
    echo "Wrong option: \"$2\""
    exit 3
  ;;

esac

set -x
cd "$CI_WORK_DIR"
bash -ex "$ALIBOT"/ci/continuous-builder.sh
echo `date` : Something failed in the continuous builder script, terminating.
exit 1
