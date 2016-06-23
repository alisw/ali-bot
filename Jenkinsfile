#!groovy

def testOnArch(architecture) {
  def testScript = '''
    # This is the build script. It is executed in bash.
    echo $SHELL
    echo $CHANGE_AUTHOR
    uname -a
    env
    ls -lR
  '''
  return { -> node("${architecture}-large") {
                dir ("alidist") { checkout scm }
                sh testScript
              }
  }
}

node {
  stage "Verify author"
  def power_users = ["ktf", "dberzano"]
  if (power_users.contains(env.CHANGE_AUTHOR)) {
    currentBuild.displayName = "Feedback required for ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
    input "Do you want to test it?"
    currentBuild.displayName = "Testing ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
    withEnv (["CHANGE_AUTHOR=${env.CHANGE_AUTHOR}"]) {
      testOnArch("slc6_x86-64")
    }
  }
  else {
    currentBuild.displayName = "Not testing ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
    throw new hudson.AbortException("Pull request does not come from a valid user")
  }
}
