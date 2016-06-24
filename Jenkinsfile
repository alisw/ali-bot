#!groovy

def testOnArch(architecture) {
  echo "Testing architecture ${architecture}"
  def testScript = '''
    # This is the build script. It is executed in bash.
    echo $SHELL
    echo $CHANGE_AUTHOR
    echo are shells executed with -e
    false
    echo apparently they are not.
    uname -a
    env
    ls -lR
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
    //currentBuild.displayName = "Feedback required for ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
    //input "Do you want to test it?"
    currentBuild.displayName = "Testing ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
  }
  else {
    currentBuild.displayName = "Not testing ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
    throw new hudson.AbortException("Pull request does not come from a valid user")
  }

  stage "Test changes"
  withEnv (["CHANGE_AUTHOR=${env.CHANGE_AUTHOR}"]) {
    testOnArch("slc6_x86-64").call()
  }
}
