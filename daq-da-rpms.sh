#!/bin/bash -ex
set -o pipefail

ALIBUILD_BRANCH=${ALIBUILD_REPO##*:}
ALIBUILD_REPO=${ALIBUILD_REPO%:*}
ALIDIST_BRANCH=${ALIDIST_REPO##*:}
ALIDIST_REPO=${ALIDIST_REPO%:*}
OVERRIDE_TAGS="AliRoot=$ALIROOT_VERSION"
DEFAULTS=daq
DISABLE=AliEn-Runtime,GEANT4_VMC,GEANT3,fastjet,GCC-Toolchain,Vc,DPMJET
REMOTE_STORE=rsync://repo.marathon.mesos/store/

function getver() {
  yum --disablerepo=rpmforge,epel info $1 | grep ^Version | cut -d: -f2 | xargs -I{} echo $1_{}
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

git clone -b $ALIBUILD_BRANCH https://github.com/$ALIBUILD_REPO alibuild && ( cd alibuild && git log --oneline -1 )
git clone -b $ALIDIST_BRANCH https://github.com/$ALIDIST_REPO alidist && ( cd alidist && git log --oneline -1 )

rpm -e mysql-libs mysql mysql-devel postfix || true
rm -rf /var/lib/mysql/
rpm --rebuilddb
yum clean all
yum install --disablerepo=rpmforge                                             \
            -y BWidget MySQL-shared MySQL-client MySQL-devel dim smi tcl-devel \
               tk-devel libcurl-devel libxml2-devel pciutils-devel mysqltcl    \
               xinetd ksh tcsh pigz MySQL-server date amore ACT daqDA-lib      \
#rpm -qa | grep ^root- | xargs -L1 rpm -e --nodeps
yum clean all
rm -fv /var/lib/rpm/__db*
rpm --rebuilddb
chmod a-w -R /var/lib/rpm/

DAQ_VERSION=`getver date`-`getver amore`-`getver ACT`-`getver daqDA-lib`
sed -i -e "s/^version:\s.*/version: \"$DAQ_VERSION\"/g" alidist/daq.sh
sed -i -e "s/^tag:\s.*/tag: \"$ALIROOT_VERSION\"/g" alidist/aliroot.sh

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

alibuild/aliBuild --reference-sources $MIRROR          \
                  --debug                              \
                  --work-dir $WORKAREA/$WORKAREA_INDEX \
                  init AliRoot
alibuild/aliBuild --reference-sources $MIRROR          \
                  --debug                              \
                  --work-dir $WORKAREA/$WORKAREA_INDEX \
                  --architecture $ARCHITECTURE         \
                  --jobs ${JOBS:-8}                    \
                  --remote-store $REMOTE_STORE::rw     \
                  --defaults $DEFAULTS                 \
                  --disable $DISABLE                   \
                  build AliRoot || BUILDERR=$?

rm -f $WORKAREA/$WORKAREA_INDEX/current_slave
[[ "$BUILDERR" != '' ]] && exit $BUILDERR

ALIROOT_PREFIX=`alibuild/alienv -w $WORKAREA/$WORKAREA_INDEX setenv AliRoot/latest -c sh -c 'echo $ALICE_ROOT'`
rsync -av $ALIROOT_PREFIX/darpms/x86_64/ $REMOTE_STORE/RPMS/DAQ/$ARCHITECTURE/
