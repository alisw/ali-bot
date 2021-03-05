#!/usr/bin/bash
set -exo pipefail

# The architecture on which to expect a published RPM, e.g. el8.x86_64.
arch=$1

lsrpm () {
  # Show published RPMs for $PACKAGE_NAME.
  s3cmd ls "s3://alibuild-repo/RPMS/$arch/alisw-${PACKAGE_NAME:?}+" |
    # Trim leading modtime, file size, URL path and file extension, leaving
    # only the package name, which we can pass to yum.
    sed 's|^.*/||; s|\.rpm$||' | sort  # comm(1) expects lines in sorted order.
}

# Store list of RPMs for this package, so we can compare later.
lsrpm > old-rpms.txt

# Upload a canary file; this will be removed by the publishing script once it's
# done, so we know when to check for our desired RPM. aliPublish has an instance
# per architecture, so make per-architecture canary files.
canary=${arch:?}/${BUILD_TAG:?}.finished
date | s3cmd put - "s3://alibuild-repo/rpmstatus/$canary"
while [ -n "$(s3cmd ls "s3://alibuild-repo/rpmstatus/$canary")" ]; do
  sleep 60
done

# Now see if the RPM we want has been published.
# Show only lines in "file 1", i.e. only new filenames.
lsrpm | comm -23 - old-rpms.txt > new-rpms.txt

# Check if there are any new RPMs.
if [ -z "$(cat new-rpms.txt)" ]; then
  echo "FAILED: aliPublish finished, but produced no new RPM for $PACKAGE_NAME" >&2
  exit 1
fi

echo "New RPMs for $PACKAGE_NAME follow:" >&2
cat new-rpms.txt >&2

# Install the repository, so we can try to install the new RPMs.
cat > /etc/yum.repos.d/alisw.repo <<EOF
[alisw]
name=ALICE Software - $arch
baseurl=https://alirepo.web.cern.ch/alirepo/RPMS/$arch
enabled=1
gpgcheck=0
EOF

# Try to install the new RPMs (don't actually install, only check for errors in
# case of an install), to see whether they're actually valid.
xargs -rtd '\n' -a new-rpms.txt yum install --assumeyes --setopt tsflags=test
yumerr=$?

if [ $yumerr -gt 0 ]; then
  echo 'FAILED: could not install new RPMs. See above for errors from yum.' >&2
else
  echo 'SUCCESS: no errors reported when installing new RPMs (dry-run only)' >&2
fi

# We don't need the downloaded RPMs any more, so clear the cache to avoid
# accumulating crud.
yum clean packages || true
exit $yumerr
