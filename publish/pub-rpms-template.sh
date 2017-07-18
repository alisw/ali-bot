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
[[ $SSLVERIFY == 0 ]] && SSLVERIFY=-k || SSLVERIFY=
which fpm
cd $TMPDIR

# Create aliswmod RPM
ALISWMOD_VERSION=1
ALISWMOD_RPM="alisw-aliswmod-$ALISWMOD_VERSION-1.%(arch)s.rpm"
if [[ ! -e "%(repodir)s/$ALISWMOD_RPM" ]]; then
  mkdir -p aliswmod/bin
  cat > aliswmod/bin/aliswmod <<EOF
#!/bin/bash -e
export MODULEPATH=$INSTALLPREFIX/$FLAVOUR/modulefiles:\$MODULEPATH
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

# Create RPM from tarball
curl -Lsf $SSLVERIFY                \
     --connect-timeout $CONNTIMEOUT \
     --retry-delay $CONNRETRYDELAY  \
     --retry $CONNRETRY "%(url)s"   | tar --strip-components=2 -xzf -
[[ -e "%(package)s/%(version)s/etc/modulefiles/%(package)s" ]]
DEPS=()
DEPS+=("--depends" "alisw-aliswmod")
for D in %(dependencies)s; do
  DEPS+=("--depends" "$D = 1-1.$FLAVOUR")
done
AFTER_INSTALL=$TMPDIR/after_install.sh
AFTER_REMOVE=$TMPDIR/after_remove.sh
cat > $AFTER_INSTALL <<EOF
#!/bin/bash -e
export WORK_DIR=$INSTALLPREFIX
cd \$WORK_DIR
export PKGPATH=$FLAVOUR/%(package)s/%(version)s
source \$PKGPATH/relocate-me.sh
mkdir -p $INSTALLPREFIX/$FLAVOUR/modulefiles/%(package)s
ln -nfs ../../%(package)s/%(version)s/etc/modulefiles/%(package)s \
        $INSTALLPREFIX/$FLAVOUR/modulefiles/%(package)s/%(version)s
mkdir -p $INSTALLPREFIX/$FLAVOUR/modulefiles/BASE
echo -e "#%%Module\nsetenv BASEDIR $INSTALLPREFIX/$FLAVOUR" > \
        $INSTALLPREFIX/$FLAVOUR/modulefiles/BASE/1.0
EOF
cat > $AFTER_REMOVE <<EOF
#!/bin/bash
( rm -f $INSTALLPREFIX/$FLAVOUR/modulefiles/%(package)s/%(version)s
  rmdir $INSTALLPREFIX/$FLAVOUR/modulefiles/%(package)s
  find $INSTALLPREFIX/$FLAVOUR/%(package)s/%(version)s -depth -type d \
       -exec rmdir '{}' \;
  rmdir $INSTALLPREFIX/$FLAVOUR/%(package)s
  if [[ "\$(find $INSTALLPREFIX/$FLAVOUR \
                 -mindepth 1 -maxdepth 1 -type d \
                 -not -name modulefiles )" == "" ]]; then
    rm -rf $INSTALLPREFIX/$FLAVOUR
    rmdir $INSTALLPREFIX
  fi
) 2> /dev/null
true
EOF
# We must put the full version in the package name to allow multiple versions
# to be installed at the same time, see [1]
# [1] http://www.rpm.org/wiki/PackagerDocs/MultipleVersions
fpm -s dir \
    -t rpm \
    --force \
    "${DEPS[@]}" \
    --prefix $INSTALLPREFIX/$FLAVOUR \
    --architecture $ARCHITECTURE \
    --version 1 \
    --iteration 1.$FLAVOUR \
    --name alisw-%(package)s+%(version)s \
    --after-install $AFTER_INSTALL \
    --after-remove $AFTER_REMOVE \
    "%(package)s/"
RPM="alisw-%(package)s+%(version)s-1-1.%(arch)s.rpm"
[[ -e $RPM ]]
mkdir -vp %(repodir)s
mv -v $RPM $ALISWMOD_RPM %(repodir)s
