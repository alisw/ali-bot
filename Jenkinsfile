#!groovy

def testAlienvOnArch(architecture) {
  def testScript = '''
    # Test scripts are executed with -e.
    set -e
    set -o pipefail
    mkdir -p /cvmfs/.modulerc || true
    [[ "$PARROT_ENABLED" != TRUE ]] && { parrot_run --mount=/cvmfs/alice.cern.ch/xbin/alienv=$PWD/ali-bot/cvmfs/alienv "$0" "$@"; exit $?; }
    OLD_VER="AliPhysics/vAN-20150131"
    NEW_VER="AliPhysics/vAN-20160622-1"
    case $ARCHITECTURE in
      slc6* ) PLATFORM=el6        ;;
      slc7* ) PLATFORM=el7        ;;
      ubt14*) PLATFORM=ubuntu1404 ;;
    esac
    PLATFORM=$PLATFORM-`uname -m`
    printf "Running on architecture $ARCHITECTURE (platform detected: $PLATFORM)\n"
    export PATH=/cvmfs/alice.cern.ch/bin:$PATH
    [[ `which alienv` == /cvmfs/alice.cern.ch/bin/alienv ]]
    printf "TEST: list AliPhysics packages\n"
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
    printf "TEST: check that the legacy AliEn package can be loaded\n"
    for METHOD in setenv printenv enter; do
      [[ `alienv_test $METHOD AliEn "which aliensh"` == *'/AliEn/'* ]]
    done
    for OVERRIDE_PLATFORM in el6 el7 ubuntu1404; do
      for METHOD in setenv printenv enter; do
        for VER in $NEW_VER $OLD_VER; do
          for TEST in cxx aliroot alien; do
            case $TEST in
              cxx)
                printf "TEST: check g++ with $VER on $OVERRIDE_PLATFORM\n"
                [[ $VER == $NEW_VER ]] && EXPECT="/cvmfs/alice.cern.ch/$OVERRIDE_PLATFORM" || EXPECT="/usr/"
                [[ `alienv_test $METHOD $VER "which g++" ALIENV_OVERRIDE_PLATFORM=$OVERRIDE_PLATFORM` == "$EXPECT"* ]]
                ;;
              aliroot)
                printf "TEST: check aliroot with $VER on $OVERRIDE_PLATFORM\n"
                [[ `alienv_test $METHOD $VER "which aliroot" ALIENV_OVERRIDE_PLATFORM=$OVERRIDE_PLATFORM` == "/cvmfs/alice.cern.ch/"*"bin/aliroot" ]]
                ;;
              alien)
                printf "TEST: check AliEn-Runtime with $VER on $OVERRIDE_PLATFORM\n"
                [[ $VER == $NEW_VER ]] && EXPECT="/AliEn-Runtime/" || EXPECT="/AliEn/"
                [[ `alienv_test $METHOD $VER "which aliensh" ALIENV_OVERRIDE_PLATFORM=$OVERRIDE_PLATFORM` == "/cvmfs/alice.cern.ch/"*"$EXPECT"* ]]
                ;;
            esac
          done
        done
      done
    done
    printf "Test was successful.\n"
  '''
  return { -> node("${architecture}-relval") {
                dir ("ali-bot") { checkout scm }
                withEnv (["ARCHITECTURE=${architecture}"]) { sh testScript }
              }
  }
}

node {
  stage "Verify author"
  def powerUsers = ["ktf", "dberzano"]
  if (!powerUsers.contains(env.CHANGE_AUTHOR)) {
    currentBuild.displayName = "Not testing ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
    throw new hudson.AbortException("Pull request does not come from a valid user")
  }

  stage "Test changes"
  dir ("ali-bot") { checkout scm }
  sh '''
    cd ali-bot
    git diff --name-only origin/$CHANGE_TARGET > ../changed_files
  '''
  def chfiles = readFile("changed_files").tokenize("\n")
  println "List of changed files: " + chfiles
  if (chfiles.contains("cvmfs/alienv") || chfiles.contains("Jenkinsfile")) {
    currentBuild.displayName = "Testing ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
    withEnv (["CHANGE_TARGET=${env.CHANGE_TARGET}"]) {
      testAlienvOnArch("slc6_x86-64").call()
    }
  }
  else {
    currentBuild.displayName = "Not checking ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
  }
}
