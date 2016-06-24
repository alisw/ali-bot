#!groovy

def testOnArch(architecture) {
  def testScript = '''
    # Test scripts are executed with -e.
    set -o pipefail
    [[ "$PARROT_ENABLED" != TRUE ]] && { parrot_run --mount=/cvmfs/alice.cern.ch/xbin/alienv=$PWD/ali-bot/cvmfs/alienv "$0" "$@"; exit $?; }
    pushd ali-bot
      git diff --name-only origin/$CHANGE_TARGET | grep -q '^cvmfs/alienv$' || { printf "No test to run, all OK."; exit 0; }
    popd
    OLD_VER="AliPhysics/vAN-20150131"
    NEW_VER="AliPhysics/vAN-20160622-1"
    echo Architecture is: $ARCHITECTURE
    case ARCHITECTURE in
      slc6*) PLATF=el6 ;;
    esac
    echo all ok
  '''
  return { -> node("${architecture}-relval") {
                dir ("ali-bot") { checkout scm }
                sh testScript
              }
  }
}

node {
  stage "Verify author"
  def power_users = ["ktf", "dberzano"]
  if (!power_users.contains(env.CHANGE_AUTHOR)) {
    currentBuild.displayName = "Not testing ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
    throw new hudson.AbortException("Pull request does not come from a valid user")
  }

  stage "Test changes"
  currentBuild.displayName = "Testing ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
  withEnv (["CHANGE_TARGET=${env.CHANGE_TARGET}",
           ]) {
    testOnArch("slc6_x86-64").call()
  }
}
