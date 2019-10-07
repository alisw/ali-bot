#!/bin/sh -ex
REPO=${REPO:-http://ali-ci.cern.ch/repo/RPMS/el7.x86_64/}
PKG=${PKG:- alisw-O2Suite+1.0.0-16}

cat << EOF >/etc/yum.repos.d/alice-test.repo
[alice-test]
name=Test Repository for alice
baseurl=$REPO
enabled=1
gpgcheck=0
EOF
yum update --disablerepo='*' --enablerepo=alice-test -y
yum search ${PKG}
yum install -y ${PKG}
