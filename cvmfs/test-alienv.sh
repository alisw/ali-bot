#!/bin/bash -e
set -o pipefail

function pe() { printf "\033[31mERROR:\033[m $1\n" >&2; }
function pi() { printf "\033[34mINFO:\033[m $1\n" >&2; }
function pt() { printf "\n\033[35m*** TEST: $1 ***\033[m\n" >&2; }
function pg() { printf "\033[32mSUCCESS:\033[m $1\n" >&2; }

# Check if required conditions to run the test are met
[[ -d .git ]] || { pe "you must run from the Git repository root"; exit 1; }

# We normally test `alienv` faked as being on CVMFS. For local tests we might
# want to override it
ALIENV="/cvmfs/alice.cern.ch/bin/alienv"     # Use CVMFS version on Travis
[[ $TRAVIS ]] || ALIENV="$PWD/cvmfs/alienv"  # Use local version everywhere else
pi "Testing alienv from $ALIENV"

# Compute relative path for a test
ALIENV_RELATIVE=
ALIENV_DIRNAME=$ALIENV
while [[ $ALIENV_DIRNAME != / ]]; do
  ALIENV_DIRNAME=$(dirname "$ALIENV_DIRNAME")
  ALIENV_RELATIVE="${ALIENV_RELATIVE}../"
done
ALIENV_RELATIVE=${ALIENV_RELATIVE:3}${ALIENV}
unset ALIENV_DIRNAME

# Define an "old" and "new" version of AliPhysics to check for. "new" has AliEn
# as dependency, "old" does not
OLD_VER="AliPhysics/vAN-20150131"
NEW_VER="AliPhysics/vAN-20170301-1"

# Overriding platform (not important for our tests)
export ALIENV_OVERRIDE_PLATFORM="el6-x86_64"
pi "Overriding platform to $ALIENV_OVERRIDE_PLATFORM"

for NP in /tmp/alienv_bin /tmp/alienv_path/bin; do
  pt "run alienv from a non-standard path ($NP) with full symlink"
  ( mkdir -p $NP
    ln -nfs "$ALIENV" $NP/alienv
    export PATH=$NP:$PATH
    ALIENV_DEBUG=1 alienv q | grep AliPhysics | tail -n1
  )
done

pt "run alienv from a non-standard path (/tmp/alienv_symlink/bin) with relative symlink"
( mkdir -p /tmp/alienv_symlink/bin
  ln -nfs "$ALIENV_RELATIVE" /tmp/alienv_symlink/bin/alienv
  export PATH=/tmp/alienv_symlink/bin:$PATH
  ALIENV_DEBUG=1 alienv q | grep AliPhysics | tail -n1
)
export PATH=$(dirname "$ALIENV"):$PATH
[[ $(which alienv) == $ALIENV ]]

pt "test package reordering"
ALIENV_DEBUG=1 alienv setenv VO_ALICE@AliEn-Runtime::v2-19-le-21,VO_ALICE@ROOT::v5-34-30-alice7-2,VO_ALICE@AliPhysics::vAN-20170301-1 -c true 2>&1 | \
  grep 'normalized to AliPhysics/vAN-20170301-1 ROOT/v5-34-30-alice7-2 AliEn-Runtime/v2-19-le-21'

pt "test checkenv command with a successful combination"
EC=0
alienv checkenv AliPhysics/vAN-20170301-1,AliRoot/v5-08-22-1 || EC=$?
[[ $EC == 0 ]] || { pe "expected 0, returned $EC"; exit 1; }

pt "test checkenv command with a faulty combination"
EC=0
{ alienv checkenv AliPhysics/vAN-20170301-1,AliPhysics/vAN-20170201-1 2>&1 | tee log.txt; } || EC=$?
[[ $EC == 1 ]] || { pe "expected 1, returned $EC"; rm -f log.txt; exit 1; }
grep -q 'conflicting version' log.txt || { pe "could not find expected output message"; rm -f log.txt; exit 1; }
rm -f log.txt

pt "test checkenv command with dependencies from multiple platforms"
EC=0
alienv checkenv AliGenerators/v20180424-1 || EC=$?
[[ $EC == 0 ]] || { pe "expected 0, returned $EC"; exit 1; }

pt "list AliPhysics packages"
alienv q | grep AliPhysics | tail -n5
function alienv_test() {
  local METHOD=$1
  local PACKAGE=$2
  local COMMAND=$3
  local OVERRIDE_ENV=$4
  case $METHOD in
    setenv)   env $OVERRIDE_ENV alienv setenv $PACKAGE -c "$COMMAND"                                                    ;;
    printenv) ( eval `env $OVERRIDE_ENV alienv printenv $PACKAGE`; $COMMAND )                                           ;;
    enter)    ( echo 'echo TEST=`'"$COMMAND"'`' | env $OVERRIDE_ENV alienv enter $PACKAGE | grep ^TEST= | cut -d= -f2-) ;;
  esac
}

pt "check that the legacy AliEn package can be loaded"
for METHOD in setenv printenv enter; do
  [[ `alienv_test $METHOD AliEn "which aliensh"` == *'/AliEn/'* ]]
done
for OVERRIDE_PLATFORM in el6 el7 ubuntu1404; do
  for METHOD in setenv printenv enter; do
    for VER in $NEW_VER $OLD_VER; do
      for TEST in cxx aliroot alien; do
        case $TEST in
          cxx)
            pt "check g++ with $VER on $OVERRIDE_PLATFORM"
            [[ $VER == $NEW_VER ]] && EXPECT="/cvmfs/alice.cern.ch/$OVERRIDE_PLATFORM" || EXPECT="/usr/"
            [[ `alienv_test $METHOD $VER "which g++" ALIENV_OVERRIDE_PLATFORM=$OVERRIDE_PLATFORM` == "$EXPECT"* ]]
            ;;
          aliroot)
            pt "check aliroot with $VER on $OVERRIDE_PLATFORM"
            [[ `alienv_test $METHOD $VER "which aliroot" ALIENV_OVERRIDE_PLATFORM=$OVERRIDE_PLATFORM` == "/cvmfs/alice.cern.ch/"*"bin/aliroot" ]]
            ;;
          alien)
            pt "check AliEn-Runtime with $VER on $OVERRIDE_PLATFORM"
            [[ $VER == $NEW_VER ]] && EXPECT="/AliEn-Runtime/" || EXPECT="/AliEn/"
            [[ `alienv_test $METHOD $VER "which aliensh" ALIENV_OVERRIDE_PLATFORM=$OVERRIDE_PLATFORM` == "/cvmfs/alice.cern.ch/"*"$EXPECT"* ]]
            ;;
        esac
      done
    done
  done
done

pg "all tests successful"
