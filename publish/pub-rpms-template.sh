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
which fpm
cd $TMPDIR
curl --silent -L "%(url)s" | tar --strip-components=2 -xzf -
DEPS=()
DEPS+=("--depends" "environment-modules")
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
mv -v $RPM %(repodir)s
