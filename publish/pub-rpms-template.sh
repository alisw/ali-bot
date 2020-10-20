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

# Create aliswmod RPM
ALISWMOD_VERSION=3
ALISWMOD_RPM="alisw-aliswmod-$ALISWMOD_VERSION-1.%(arch)s.rpm"
if [[ ! -e "%(repodir)s/$ALISWMOD_RPM" ]]; then
  mkdir -p aliswmod/bin
  mkdir -p aliswmod/etc/profile.d
  cat > aliswmod/etc/profile.d/99-aliswmod.sh << \EOF
export LD_LIBRARY_PATH=/opt/alisw/el7/lib:/opt/alisw/el7/lib64:$LD_LIBRARY_PATH
export PATH=/opt/alisw/el7/bin:$PATH
EOF
  cat > aliswmod/bin/aliswmod <<EOF
#!/bin/bash -e
export MODULEPATH=$INSTALLPREFIX/$FLAVOUR/modulefiles:$INSTALLPREFIX/$FLAVOUR/etc/Modules/modulefiles:\$MODULEPATH
EOF
  cat >> aliswmod/bin/aliswmod <<\EOF
MODULES_SHELL=$(ps -e -o pid,command | grep -E "^\s*$PPID\s+" | awk '{print $2}' | sed -e 's/^-\{0,1\}\(.*\)$/\1/')
IGNORE_ERR="Unable to locate a modulefile for 'Toolchain/"
[[ $MODULES_SHELL ]] || MODULES_SHELL=bash
MODULES_SHELL=${MODULES_SHELL##*/}
if [[ $1 == enter ]]; then
  shift
  eval "$((printf '' >&2; modulecmd bash load "$@") 2> >(grep -v "$IGNORE_ERR" >&2))"
  exec $MODULES_SHELL -i
fi
(printf '' >&2; modulecmd $MODULES_SHELL "$@") 2> >(grep -v "$IGNORE_ERR" >&2)
EOF
  chmod 0755 aliswmod/bin/aliswmod
  pushd aliswmod
    fpm -s dir                        \
        -t rpm                        \
        --force                       \
        --depends environment-modules \
        --prefix /                    \
        --architecture $ARCHITECTURE  \
        --version $ALISWMOD_VERSION   \
        --iteration 1.$FLAVOUR        \
        --name alisw-aliswmod         \
        .
  popd
  mv aliswmod/$ALISWMOD_RPM .
  rm -rf aliswmod/
else
  echo No need to create the package, skipping
  ALISWMOD_RPM=
fi

DEPS=()
DEPS+=("--depends" "alisw-aliswmod >= $ALISWMOD_VERSION")

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
  # Make sure Modulefile is there
  [[ -e "$RPM_ROOT/etc/modulefiles/%(package)s" ]]
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
  # Remove useless files conflicting between packages
  if [ ! "X$RPM_IS_UPDATABLE" = X ]; then
    rm -rfv $RPM_ROOT/.build-hash            \
            $RPM_ROOT/.rpm-extra-deps        \
            $RPM_ROOT/etc/profile.d/init.sh* \
            $RPM_ROOT/.original-unrelocated
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
  sed -e 's|%(package)s/\$version||g; s|%(package)s/%(version)s||g' $INSTALLPREFIX/$FLAVOUR/etc/modulefiles/%(package)s > \$MODULE_DEST_DIR/%(package)s/%(version)s
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

# We must put the full version in the package name to allow multiple versions
# to be installed at the same time, see [1]
# [1] http://www.rpm.org/wiki/PackagerDocs/MultipleVersions
pushd unpack_rpm
  fpm -s dir                           \
      -t rpm                           \
      --force                          \
      "${DEPS[@]}"                     \
      --prefix $INSTALLPREFIX/$FLAVOUR \
      --architecture $ARCHITECTURE     \
      --version "$RPM_VERSION"         \
      --iteration 1.$FLAVOUR           \
      --name "$RPM_PACKAGE"            \
      --exclude compile_commands.json  \
      --after-install $AFTER_INSTALL   \
      --after-remove $AFTER_REMOVE     \
      "$RPM_UNPACK_DIR"
  RPM="${RPM_PACKAGE}-${RPM_VERSION}-1.%(arch)s.rpm"
  [[ -e $RPM ]]
  mkdir -vp %(repodir)s
  mv -v $RPM ../
popd
mv -v $RPM $ALISWMOD_RPM %(stagedir)s
