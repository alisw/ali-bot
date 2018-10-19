#!/bin/bash -ex
set -o pipefail

# Check for required variables
for V in ALIDIST_REPO ALIBUILD_REPO ALIROOT_VERSION NODE_NAME; do
  [[ $(eval echo \$$V) ]] || { echo "Required variable $V not defined!"; ERR=1; continue; }
  eval "export $V"
done
[[ $ERR == 1 ]] && exit 1 || true

ALIBUILD_BRANCH=${ALIBUILD_REPO##*:}
ALIBUILD_REPO=${ALIBUILD_REPO%:*}
ALIDIST_BRANCH=${ALIDIST_REPO##*:}
ALIDIST_REPO=${ALIDIST_REPO%:*}
OVERRIDE_TAGS="AliRoot=$ALIROOT_VERSION"
DEFAULTS=daq
REMOTE_STORE=rsync://repo.marathon.mesos/store/
YUM_DISABLEREPO=rpmforge,epel,cernvm

function getver() {
  local PKGVER
  PKGVER=$(yum ${YUM_DISABLEREPO:+--disablerepo $YUM_DISABLEREPO} info $1 | grep ^Version | cut -d: -f2 | xargs -I{} echo $1_{})
  [[ $PKGVER ]] && echo $PKGVER || echo ERROR
}

cat > /etc/yum.repos.d/yum-alice-daq.slc6_64.repo <<EoF
[main]
[alice-daq]
name=ALICE DAQ software and dependencies - SLC6/64
baseurl=https://yum:daqsoftrpm@alice-daq-yum.web.cern.ch/alice-daq-yum/slc6_64/
enabled=1
protect=1
gpgcheck=0
EoF

# User alicedaq and group z2 required by RPMs...
getent group z2 || groupadd z2 -g 10000
getent passwd alicedaq || useradd alicedaq -u 10000 -g 10000

# Clone alidist
git clone -b $ALIDIST_BRANCH https://github.com/$ALIDIST_REPO alidist
echo "alidist version" ; ( cd alidist && git log --oneline -1 )

# Use aliBuild from pip (install in a temporary directory)
# Note: using pip ensures all relevant dependencies are installed as well
export PYTHONUSERBASE=$(mktemp -d)
export PATH=$PYTHONUSERBASE/bin:$PATH
export LD_LIBRARY_PATH=$PYTHONUSERBASE/lib:$LD_LIBRARY_PATH
pip install --user git+https://github.com/${ALIBUILD_REPO}${ALIBUILD_BRANCH:+@$ALIBUILD_BRANCH}
type aliBuild

rpm -e mysql-libs mysql mysql-devel postfix || true
rm -rf /var/lib/mysql/
rpm --rebuilddb
yum clean all
yum install ${YUM_DISABLEREPO:+--disablerepo $YUM_DISABLEREPO}                 \
            -y BWidget MySQL-shared MySQL-client MySQL-devel dim smi tcl-devel \
               tk-devel libcurl-devel libxml2-devel pciutils-devel mysqltcl    \
               xinetd ksh tcsh pigz MySQL-server date amore ACT daqDA-lib
yum clean all
rm -fv /var/lib/rpm/__db*
rpm --rebuilddb
chmod a-w -R /var/lib/rpm/

DAQ_VERSION=$(getver date)-$(getver amore)-$(getver ACT)-$(getver daqDA-lib)
if [[ $DAQ_VERSION == *ERROR* ]]; then
  echo "Error getting version of Yum packages"
  exit 1
fi
sed -i -e "s/^version:\s.*/version: \"$DAQ_VERSION\"/g" alidist/daq.sh
sed -i -e "s/^tag:\s.*/tag: \"$ALIROOT_VERSION\"/g" alidist/aliroot.sh

pushd alidist &> /dev/null
  git diff
popd &> /dev/null

BUILD_DATE=$(echo 2015$(echo "$(date -u +%s) / (86400 * 3)" | bc))

MIRROR=/build/mirror
WORKAREA=/build/workarea/sw/$BUILD_DATE
WORKAREA_INDEX=0

CURRENT_SLAVE=unknown
while [[ "$CURRENT_SLAVE" != '' ]]; do
  WORKAREA_INDEX=$((WORKAREA_INDEX+1))
  CURRENT_SLAVE=$(cat $WORKAREA/$WORKAREA_INDEX/current_slave 2> /dev/null || true)
  [[ "$CURRENT_SLAVE" == "$NODE_NAME" ]] && CURRENT_SLAVE=
done

mkdir -p $WORKAREA/$WORKAREA_INDEX
echo $NODE_NAME > $WORKAREA/$WORKAREA_INDEX/current_slave

# AliRoot in development mode. Prevents unnecessarily creating/uploading tarball
# by still allowing ROOT build to be cached
rm -rf AliRoot/
aliBuild --reference-sources $MIRROR \
         --defaults $DEFAULTS        \
         init AliRoot

FETCH_REPOS="$(aliBuild build --help | grep fetch-repos || true)"
aliBuild --reference-sources $MIRROR          \
         --debug                              \
         --work-dir $WORKAREA/$WORKAREA_INDEX \
         --architecture $ARCHITECTURE         \
         --jobs ${JOBS:-8}                    \
         ${FETCH_REPOS:+--fetch-repos}        \
         --remote-store $REMOTE_STORE::rw     \
         --defaults $DEFAULTS                 \
         build AliRoot || BUILDERR=$?

rm -f $WORKAREA/$WORKAREA_INDEX/current_slave
[[ "$BUILDERR" != '' ]] && exit $BUILDERR

ALIROOT_PREFIX=$(alienv -w $WORKAREA/$WORKAREA_INDEX setenv AliRoot/latest -c sh -c 'echo $ALICE_ROOT')
rsync -av $ALIROOT_PREFIX/darpms/x86_64/ $REMOTE_STORE/RPMS/DAQ/$ARCHITECTURE/
