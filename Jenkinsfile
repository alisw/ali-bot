#!groovy

def testAlienvOnArch(architecture) {
  def testScript = '''
    # Test scripts are executed with -e.
    set -e
    set -o pipefail
    mkdir -p /cvmfs/.modulerc || true
    [[ "$PARROT_ENABLED" != TRUE ]] && { parrot_run --mount=/cvmfs/alice.cern.ch/bin/alienv=$PWD/ali-bot/cvmfs/alienv "$0" "$@"; exit $?; }
    OLD_VER="AliPhysics/vAN-20150131"
    NEW_VER="AliPhysics/vAN-20160622-1"
    case $ARCHITECTURE in
      slc6* ) PLATFORM=el6        ;;
      slc7* ) PLATFORM=el7        ;;
      ubt14*) PLATFORM=ubuntu1404 ;;
    esac
    PLATFORM=$PLATFORM-`uname -m`
    printf "Running on architecture $ARCHITECTURE (platform detected: $PLATFORM)\n"
    for NP in /tmp/alienv_bin /tmp/alienv_path/bin; do
      printf "TEST: run alienv from a non-standard path ($NP) with full symlink\n"
      ( mkdir -p $NP
        ln -nfs /cvmfs/alice.cern.ch/bin/alienv $NP/alienv
        export PATH=$NP:$PATH
        ALIENV_DEBUG=1 alienv q | grep AliPhysics | tail -n1
      )
    done
    printf "TEST: run alienv from a non-standard path (/tmp/alienv_symlink/bin) with relative symlink\n"
    ( mkdir -p /tmp/alienv_symlink/bin
      ln -nfs ../../../cvmfs/alice.cern.ch/bin/alienv /tmp/alienv_symlink/bin/alienv
      export PATH=/tmp/alienv_symlink/bin:$PATH
      ALIENV_DEBUG=1 alienv q | grep AliPhysics | tail -n1
    )
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

def testPublish() {
  def testScript = '''
    set -e
    set -x
    cd ali-bot/publish
    for TESTFILE in test*.yaml; do
      CONF=${TESTFILE//test}
      CONF=aliPublish${CONF%.*}.conf
      echo "==> Testing rules from $TESTFILE using configuration $CONF"
      [[ -e $TESTFILE && -e $CONF ]]
      ./aliPublish test-rules --test-conf $TESTFILE --conf $CONF
    done
  '''
  return { -> node("slc7_x86-64-large") {
                dir ("ali-bot") { checkout scm }
                sh testScript
              }
  }
}

node {
  stage "Check changeset"
  dir ("ali-bot") { checkout scm }
  def chfiles = []
  if (env.CHANGE_TARGET != null) {
    withEnv (["CHANGE_TARGET=${env.CHANGE_TARGET}"]) {
      sh '''
        cd ali-bot
        git diff --name-only origin/$CHANGE_TARGET > ../changed_files
      '''
    }
    chfiles = readFile("changed_files").tokenize("\n")
  }
  println "List of changed files: " + chfiles
  def listAlienv  = [ "Jenkinsfile",
                      "cvmfs/alienv" ]
  def listPublish = [ "publish/aliPublish",
                      "publish/aliPublish.conf",
                      "publish/aliPublish-titan.conf",
                      "publish/aliPublish-nightlies.conf",
                      "publish/test.yaml",
                      "publish/test-titan.yaml",
                      "publish/test-nightlies.yaml" ]
  def jobs = [:]
  for (String f : listAlienv) {
    if (chfiles.contains(f)) {
      jobs << [ "alienv": testAlienvOnArch("slc6_x86-64") ]
    }
  }
  for (String f : listPublish) {
    if (chfiles.contains(f)) {
      jobs << [ "publish": testPublish() ]
    }
  }

  if (jobs.size() > 0) {

    stage "Verify author"
    def powerUsers = ["ktf", "dberzano"]
    if (!powerUsers.contains(env.CHANGE_AUTHOR)) {
      currentBuild.displayName = "Feedback required for ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
      input "Change comes from user ${env.CHANGE_AUTHOR}, do you want to test it?"
    }

    stage "Test changes"
    currentBuild.displayName = "Testing ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
    parallel(jobs)

  }
  else {
    currentBuild.displayName = "Check unneeded for ${env.BRANCH_NAME} (${env.CHANGE_AUTHOR})"
  }
}
