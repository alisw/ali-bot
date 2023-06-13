#!/bin/bash -ex
set -o pipefail
INSTALLPREFIX=/opt/alisw
TMPDIR="%(workdir)s"
FULLVER="%(version)s"
VERSION=${FULLVER%%-*}
REVISION=${FULLVER##*-}
FULLARCH="%(arch)s"
FLAVOUR=${FULLARCH%%.*}
ARCHITECTURE=${FULLARCH##*.}
SSLVERIFY=%(http_ssl_verify)d
CONNTIMEOUT=%(conn_timeout_s)d
CONNRETRY=%(conn_retries)d
CONNRETRYDELAY=%(conn_dethrottle_s)d
RPM_IS_UPDATABLE=%(updatable)s
[[ $SSLVERIFY == 0 ]] && SSLVERIFY=-k || SSLVERIFY=
which fpm
cd $TMPDIR

if [[ $RPM_IS_UPDATABLE ]]; then
  case "%(dependencies)s" in
    *AliEn-Runtime*)
      echo "Not publishing %(package)s with version %(version)s as it has AliEn-Runtime as a dependency"
      exit 0 ;;
  esac
fi

DEPS=()
# Use env modules v4 instead of aliswmod
DEPS+=("--depends" "environment-modules >= 4.0")

# Updatable RPMs don't have the version number hardcoded in the package name
if [[ $RPM_IS_UPDATABLE ]]; then
  RPM_VERSION="%(version)s"
  RPM_VERSION=${RPM_VERSION//-/_}
  RPM_PACKAGE="alisw-%(package)s"
  RPM_TAR_STRIP=4
  RPM_ROOT="."
  RPM_UNPACK_DIR="."
  RPM_MODULEFILE_PREFIX=
  for D in %(dependencies)s; do
    DEP_NAME=${D%%+*}
    DEP_VER=${D#*+}
    DEP_VER=${DEP_VER//-/_}
    DEPS+=("--depends" "$DEP_NAME >= ${DEP_VER}-1.$FLAVOUR")
  done
else
  RPM_VERSION=1
  RPM_PACKAGE="alisw-%(package)s+%(version)s"
  RPM_TAR_STRIP=2
  RPM_ROOT="%(package)s/%(version)s"
  RPM_UNPACK_DIR="%(package)s"
  for D in %(dependencies)s; do
    DEPS+=("--depends" "$D = 1-1.$FLAVOUR")
  done
fi

# Create RPM from tarball
mkdir -p unpack_rpm
pushd unpack_rpm
  # RPM's root dir will be $TMPDIR/unpack_rpm/$RPM_UNPACK_DIR
  curl -Lsf $SSLVERIFY                \
       --connect-timeout $CONNTIMEOUT \
       --retry-delay $CONNRETRYDELAY  \
       --retry $CONNRETRY "%(url)s"   | tar --strip-components=$RPM_TAR_STRIP -xzf -
  # Rename "bad" files -- see https://github.com/jordansissel/fpm/issues/1385
  while read BAD_FILE; do
    mv -v "$BAD_FILE" "${BAD_FILE//\'/_}"
  done < <(find . -name "*'*" || true)
  # Add extra dependencies, if applicable
  if [[ -e $RPM_ROOT/.rpm-extra-deps ]]; then
    OLD_IFS="$IFS"
    IFS=$'\n'
    for D in $(cat "$RPM_ROOT/.rpm-extra-deps" | sed -e 's/[ ]*#.*//;s/ *\(.*\) */\1/g; /^$/d'); do
      DEPS+=("--depends" "$D")
    done
    IFS="$OLD_IFS"
  fi
popd

AFTER_INSTALL=$TMPDIR/after_install.sh
AFTER_REMOVE=$TMPDIR/after_remove.sh

cat > $AFTER_INSTALL <<EOF
#!/bin/bash -e
RPM_IS_UPDATABLE=$RPM_IS_UPDATABLE
export WORK_DIR=$INSTALLPREFIX
cd \$WORK_DIR
[[ \$RPM_IS_UPDATABLE ]] && export PKGPATH=${FLAVOUR} || export PKGPATH="${FLAVOUR}/%(package)s/%(version)s"
EOF
grep -v 'profile\.d/init\.sh\.unrelocated' unpack_rpm/$RPM_ROOT/relocate-me.sh >> $AFTER_INSTALL
rm -fv unpack_rpm/$RPM_ROOT/relocate-me.sh
cat >> $AFTER_INSTALL <<EOF
MODULE_DEST_DIR=$INSTALLPREFIX/$FLAVOUR/\${RPM_IS_UPDATABLE:+etc/Modules/}modulefiles
mkdir -p \$MODULE_DEST_DIR/%(package)s
if [[ \$RPM_IS_UPDATABLE ]]; then
  sed 's|%(package)s/\$version||g; s|%(package)s/%(version)s||g' \\
      "$INSTALLPREFIX/$FLAVOUR/%(package)s/etc/modulefiles/%(package)s" \\
      > "\$MODULE_DEST_DIR/%(package)s/%(version)s"
else
  ln -nfs ../../%(package)s/%(version)s/etc/modulefiles/%(package)s \$MODULE_DEST_DIR/%(package)s/%(version)s
fi
mkdir -p \$MODULE_DEST_DIR/BASE
echo -e "#%%Module\nsetenv BASEDIR $INSTALLPREFIX/$FLAVOUR" > \$MODULE_DEST_DIR/BASE/1.0
EOF

cat > $AFTER_REMOVE <<EOF
#!/bin/bash
( rm -f $INSTALLPREFIX/$FLAVOUR/modulefiles/%(package)s/%(version)s
  rm -f $INSTALLPREFIX/$FLAVOUR/etc/Modules/modulefiles/%(package)s/%(version)s
  rmdir $INSTALLPREFIX/$FLAVOUR/modulefiles/%(package)s
  find $INSTALLPREFIX/$FLAVOUR/%(package)s/%(version)s -depth -type d -exec rmdir '{}' \;
  rmdir $INSTALLPREFIX/$FLAVOUR/%(package)s
  if [[ "\$(find $INSTALLPREFIX/$FLAVOUR -mindepth 1 -maxdepth 1 -type d -not -name modulefiles )" == "" ]]; then
    rm -rf $INSTALLPREFIX/$FLAVOUR
    rmdir $INSTALLPREFIX
  fi
) 2> /dev/null
true
EOF

# For non-updatable RPMs, we must put the full version in the package name to
# allow multiple versions to be installed at the same time:
# https://rpm.org/user_doc/multiple_versions.html

# We want updatable RPMs to be installed under /opt/alisw/el8/GenTopo, not
# directly under /opt/alisw/el8. However, modulefiles should still go in
# /opt/alisw/el8/Modules/modulefiles. (These are installed in $AFTER_INSTALL).
pushd unpack_rpm
  fpm -s dir -t rpm --force "${DEPS[@]}" --architecture "$ARCHITECTURE"       \
      --name "$RPM_PACKAGE" --version "$RPM_VERSION" --iteration "1.$FLAVOUR" \
      --prefix "$INSTALLPREFIX/$FLAVOUR${RPM_IS_UPDATABLE:+/%(package)s}"     \
      --after-install "$AFTER_INSTALL" --after-remove "$AFTER_REMOVE"         \
      --exclude compile_commands.json "$RPM_UNPACK_DIR"
  RPM="${RPM_PACKAGE}-${RPM_VERSION}-1.%(arch)s.rpm"
  [[ -e $RPM ]]
  mkdir -vp "%(repodir)s"
  mv -v "$RPM" ../
popd
mv -v "$RPM" "%(stagedir)s"
