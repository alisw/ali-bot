#!groovy

def testOnArch(architecture) {
  def testScript = '''
    # Test scripts are executed with -e.
    set -o pipefail
    parrot_run --mount=/cvmfs/alice.cern.ch/bin/alienv=$PWD/ali-bot/cvmfs/alienv   \
               /cvmfs/alice.cern.ch/bin/alienv q | grep -i aliphysics | tail -n 10
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
  withEnv (["CHANGE_AUTHOR=${env.CHANGE_AUTHOR}"]) {
    testOnArch("slc6_x86-64").call()
  }
}
