#!groovy

def testOnArch(architecture) {
  echo "Testing architecture ${architecture}"
  def testScript = '''
    # Test scripts are executed with -e.
    parrot_run /cvmfs/alice.cern.ch/bin/alienv q | grep -i aliphysics | tail -n 10
  '''
  echo "Testing architecture ${architecture}: ended"
  return { -> node("${architecture}-relval") {
                dir ("ali-bot") { checkout scm }
                sh testScript
              }
  }
}

node {
  stage "Verify author"
  def power_users = ["ktf", "dberzano"]
  if (power_users.contains(env.CHANGE_AUTHOR)) {
    currentBuild.displayName = "Not testing ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
    return false
    //throw new hudson.AbortException("Pull request does not come from a valid user")
  }

  stage "Test changes"
  currentBuild.displayName = "Testing ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
  withEnv (["CHANGE_AUTHOR=${env.CHANGE_AUTHOR}"]) {
    testOnArch("slc6_x86-64").call()
  }
}
