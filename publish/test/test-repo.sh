#!/bin/sh -ex
REPO=${REPO:-https://alirepo.web.cern.ch/alirepo/RPMS/el7.x86_64}
PKG=${PKG:-alisw-flpproto+v0.9.2-9.x86_64}

curl -I $REPO/repodata/repomd.xml
cat << EOF >/etc/yum.repos.d/alice-test.repo
[alice-test]
name=Test Repository for alice
baseurl=$REPO
enabled=1
gpgcheck=0
EOF
yum update --disablerepo='*' --enablerepo=alice-test -y
yum list available | grep alisw
yum search rpm-test
yum search flpsuite
yum install -y ${PKG}
