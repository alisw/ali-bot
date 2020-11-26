#!/bin/sh -e

# gen-repo-report.sh -- Generate text report on aliBuild repository usage
#
# Prepare the report in a single temporary file and atomically mv it at the end
# Report format: space-separated entries, two types of lines: store, dist.
#
# store ubt1604_x86-64 258577773    1466594777           ./9c/9c85a4b959b4f83e9cbd8f6a59d6e1f944f5c632/GCC-Toolchain-v4.9.3-next-1.ubt1604_x86-64.tar.gz
#       [architecture] [size_bytes] [creation_timestamp] [package_name]
#
# dist ubt1604_x86-64 ./ROOT/ROOT-v5-34-30-next-alice-1/MonALISA-gSOAP-client-v2.7.10-1.ubt1604_x86-64.tar.gz
#      [architecture] [package_name]
#
# Notes:
#
# * All times are Unix timestamps in UTC
# * Consecutive spaces are treated as a single space
# * Lines are stripped before parsing
# * The filename field is the last one and may contain spaces

[ ! -d "$TARBALLS_PREFIX" ] && { echo "Cannot find tarballs repo $TARBALLS_PREFIX"; exit 1; }
[ ! -d "$TEMP_RESULTS" ] && { echo "Cannot find temporary dir $TEMP_RESULTS"; exit 1; }

find "$TARBALLS_PREFIX" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' | while read -r A; do
  mkdir -p "$TARBALLS_PREFIX/$A/store" "$TARBALLS_PREFIX/$A/dist"
  find "$TARBALLS_PREFIX/$A/store" -type f -printf "store $A %s %Cs ./%P\n" >> "$TEMP_RESULTS/repo-report.txt"
  find "$TARBALLS_PREFIX/$A/dist" -type l -printf "dist $A ./%P\n" >> "$TEMP_RESULTS/repo-report.txt"
done
