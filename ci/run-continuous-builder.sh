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
for V in GITHUB_TOKEN GITLAB_USER GITLAB_PASS PR_REPO PACKAGE CHECK_NAME PR_BRANCH ALIBUILD_DEFAULTS INFLUXDB_WRITE_URL; do
  [[ $(eval echo \$$V) ]] || { echo "Required variable $V not defined!"; ERR=1; continue; }
  eval "export $V"
done
[[ $ERR == 1 ]] && exit 1

JOBS_DEFAULT=$(sysctl -n hw.ncpu || echo 4)
[[ `uname` == Darwin ]] && OS=macos || OS=linux
export CI_NAME=$(echo ${PR_REPO_CHECKOUT:-$PACKAGE}|tr '[[:upper:]]' '[[:lower:]]')_checker_${OS}_${ALIBUILD_DEFAULTS}_ci

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
export PR_REPO_CHECKOUT="$PACKAGE"
export JOBS=${JOBS-$JOBS_DEFAULT}
export ALIBUILD_O2_TESTS=1
export ALIBUILD_REPO="alisw/alibuild"
export MONALISA_HOST=aliendb9.cern.ch
export MONALISA_PORT=8885
export MAX_DIFF_SIZE=20000000
export DELAY=20
export DEBUG=true
export MIRROR=/build/mirror

# Setup working directory
ALIBOT="$(cd ..;pwd)"
CI_WORK_DIR=/build/ci_checks/${CI_NAME}_${WORKER_INDEX}
export PYTHONUSERBASE="$CI_WORK_DIR/python_local"
export PATH="$PYTHONUSERBASE/bin:$PATH"
mkdir -p "$CI_WORK_DIR" "$PYTHONUSERBASE"
pip install --user -e "$ALIBOT"
set -x
cd "$CI_WORK_DIR"
[[ -d alidist/.git ]]             || git clone https://github.com/alisw/alidist
[[ -d alibuild/.git ]]            || git clone https://github.com/alisw/alibuild
[[ -d "$PR_REPO_CHECKOUT/.git" ]] || git clone "https://github.com/$PR_REPO" "$PR_REPO_CHECKOUT"

if [[ $2 != --test* ]]; then
  ( cd alidist;           git fetch --all; git checkout master;       git reset --hard origin/master; )
  ( cd alibuild;          git fetch --all; git checkout master;       git reset --hard origin/master; )
  ( cd $PR_REPO_CHECKOUT; git fetch --all; git checkout "$PR_BRANCH"; git reset --hard "origin/$PR_BRANCH"; )
else
  echo "Test mode: will not update repositories" >&2
fi

case "$2" in

  --list)
    set -x
    $ALIBOT/list-branch-pr --show-main-branch --check-name $CHECK_NAME ${TRUST_COLLABORATORS:+--trust-collaborators} ${TRUSTED_USERS:+--trusted $TRUSTED_USERS} $PR_REPO@$PR_BRANCH ${WORKERS_POOL_SIZE:+--workers-pool-size $WORKERS_POOL_SIZE} ${WORKER_INDEX:+--worker-index $WORKER_INDEX} ${DELAY:+--max-wait $DELAY}
    exit 0
  ;;

  --test-build)
    set -x
    PACKAGE=${3-$PACKAGE}
    alibuild/aliBuild init $PACKAGE --defaults $ALIBUILD_DEFAULTS --reference-source $MIRROR
    exec alibuild/aliBuild build $PACKAGE --defaults $ALIBUILD_DEFAULTS ${DEBUG:+--debug} --reference-source $MIRROR
  ;;

  --test-doctor)
    set -x
    PACKAGE=${3-$PACKAGE}
    exec alibuild/aliDoctor $PACKAGE --defaults $ALIBUILD_DEFAULTS ${DEBUG:+--debug}
  ;;

  "")
  ;;

  *)
    echo "Wrong option: \"$2\""
    exit 3
  ;;

esac

# Not in a screen? Switch to a screen!
[[ $STY ]] || exec screen -dmS ${CI_NAME}_${WORKER_INDEX} "$PROG" "$@"

# Production command
while ! bash -ex "$ALIBOT"/ci/continuous-builder.sh; do
  echo Something failed in the continuous builder script, restarting in 10 seconds...
  sleep 10
done
