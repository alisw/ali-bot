#!/bin/bash -e

# gen-repo-report.sh -- Generate text report on aliBuild repository usage
#
# Prepare the report in a single temporary file and atomically mv it at the end
# Report format: space-separated entries, three types of lines: store, dist,
# info.
#
# store ubt1604_x86-64 258577773    1466594777           ./9c/9c85a4b959b4f83e9cbd8f6a59d6e1f944f5c632/GCC-Toolchain-v4.9.3-next-1.ubt1604_x86-64.tar.gz
#       [architecture] [size_bytes] [creation_timestamp] [package_name]
#
# dist ubt1604_x86-64 ./ROOT/ROOT-v5-34-30-next-alice-1/MonALISA-gSOAP-client-v2.7.10-1.ubt1604_x86-64.tar.gz
#      [architecture] [package_name]
#
# info 1531140052   1531140054 2
#      [start_time] [end_time] [seconds_taken]
#
# Notes:
#
# * All times are Unix timestamps in UTC
# * Consecutive spaces are treated as a single space
# * Lines are stripped before parsing
# * The filename field is the last one and may contain spaces

cd /

if [[ ! -d $TARBALLS_PREFIX ]]; then
  echo "Cannot find tarballs repo $TARBALLS_PREFIX"
  exit 1
fi

if [[ ! -d $TEMP_RESULTS ]]; then
  echo "Cannot find temporary dir $TEMP_RESULTS"
  exit 1
fi

ARCHS=($(cd $TARBALLS_PREFIX && ls -1d *))
T_REPORT=$TEMP_RESULTS/repo-report.txt

T0=$(date -u +%s)
for A in ${ARCHS[@]}; do
  [ ! -d $TARBALLS_PREFIX/$A ] && continue
  pushd $TARBALLS_PREFIX/$A > /dev/null
    [[ -d store ]] || mkdir store
    [[ -d dist ]] || mkdir dist

    pushd store > /dev/null
      find . -type f -printf "store $A %s %Cs %p\n" >> $T_REPORT
    popd > /dev/null

    pushd dist > /dev/null
      find . -type l -printf "dist $A %p\n" >> $T_REPORT
    popd > /dev/null

  popd > /dev/null
done
T1=$(date -u +%s)

echo "info $T0 $T1 $((T1-T0))" >> $T_REPORT
echo Produced $T_REPORT in $((T1-T0)) seconds
