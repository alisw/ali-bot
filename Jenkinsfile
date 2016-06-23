#!groovy

def buildAny(architecture) {
  def build_script = '''
      # Make sure we have only one builder per directory
      BUILD_DATE=$(echo 2015$(echo "$(date -u +%s) / (86400 * 3)" | bc))
      WORKAREA=/build/workarea/$WORKAREA_PREFIX/$BUILD_DATE

      CURRENT_SLAVE=unknown
      while [[ "$CURRENT_SLAVE" != '' ]]; do
        WORKAREA_INDEX=$((WORKAREA_INDEX+1))
        CURRENT_SLAVE=$(cat $WORKAREA/$WORKAREA_INDEX/current_slave 2> /dev/null || true)
        [[ "$CURRENT_SLAVE" == "$NODE_NAME" ]] && CURRENT_SLAVE=
      done

      mkdir -p $WORKAREA/$WORKAREA_INDEX
      echo $NODE_NAME > $WORKAREA/$WORKAREA_INDEX/current_slave

      (cd alidist && git show)
      rm -fr alibuild
      git clone https://github.com/alisw/alibuild

      # Whenever we change a spec file, we rebuild it and then we
      # rebuild AliRoot just to make sure we did not break anything.
      case $CHANGE_TARGET in
        null)
          PKGS=AliPhysics
        ;;
        *)
          PKGS=`cd alidist ; git diff --name-only origin/$CHANGE_TARGET | grep .sh | sed -e's|[.]sh$||'`
        ;;
      esac

      for p in $PKGS; do
        # Euristics to decide which kind of test we should run.
        case $p in
          # Packages which only touch rivet
          yoda|rivet)
            BUILD_TEST="$BUILD_TEST Rivet-test" ;;

          # Packages which only touch O2
          o2|fairroot|dds|zeromq|nanomsg|sodium|pythia|pythia6|lhapdf)
            BUILD_TEST="$BUILD_TEST O2 " ;;

          # Packages which are only for AliRoot
          aliphysics|aliroot-test)
            BUILD_TEST="$BUILD_TEST AliRoot-test" ;;

          # Packages which are common between O2 and Rivet
          python-modules|python|freetype|libpng|hepmc)
            BUILD_TEST="$BUILD_TEST Rivet-test" ;; # FIXME: For the moment we test only Rivet

          # Packages which are for AliRoot and O2
          aliroot|geant4|geant4_vmc|geant3)
            BUILD_TEST="$BUILD_TEST AliRoot-test" ;; # FIXME: For the moment we test only AliRoot

          # Packages which are (will be) common for all of them
          gcc-toolchain|root|cmake|zlib|alien-runtime|gsl|boost|cgal|fastjet)
            BUILD_TEST="$BUILD_TEST AliRoot-test Rivet-test" ;;

          # Packages which are standalone
          *) BUILD_TEST="$BUILD_TEST $p" ;;
        esac
      done

      for p in `echo $BUILD_TEST | sort -u`; do
        alibuild/aliBuild --work-dir $WORKAREA/$WORKAREA_INDEX                                 \
                          --reference-sources /build/mirror                                    \
                          --debug                                                              \
                          --jobs 16                                                            \
                          --disable DDS                                                        \
                          --remote-store rsync://repo.marathon.mesos/store/${DO_UPLOAD:+::rw}  \
                          -d build $p || BUILDERR=$?
      done

      rm -f $WORKAREA/$WORKAREA_INDEX/current_slave
      if [ ! "X$BUILDERR" = X ]; then
        exit $BUILDERR
      fi
    '''
  return { -> node("${architecture}-large") {
                dir ("alidist") { checkout scm }
                sh build_script
              }
  }
}

node {
  stage "Verify author"
  def power_users = ["ktf", "dberzano"]
  if (power_users.contains(env.CHANGE_AUTHOR)) {
    currentBuild.displayName = "Testing ${env.BRANCH_NAME} from ${env.CHANGE_AUTHOR}"
    echo "PR comes from power user ${env.CHANGE_AUTHOR} and it affects ${env.CHANGE_TARGET}"
  }
}
