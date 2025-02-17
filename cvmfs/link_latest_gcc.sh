#!/bin/bash
set -eo pipefail
GCC_VER=${1:-"v13.2.0"}

[[ $(pwd) == /cvmfs/alice.cern.ch ]] || { echo "Not in /cvmfs/alice.cern.ch, aborting"; exit 1; }

function pretty_print() {
    printf "\033[1;34m==>\033[m \033[1m%s\033[m\n" "$1"
}

function print_green() {
    printf "\033[1;32m%s\033[m\n" "$1"
}

function print_red() {
    printf "\033[1;31m%s\033[m\n" "$1"
}

for D in el* ubuntu*; do
  [[ -d "$D" ]] || continue
  pretty_print "Updating latest GCC $GCC_VER for arch $D"
  LATEST=$(find "$D"/Modules/modulefiles/GCC-Toolchain/*"${GCC_VER}"* -mindepth 0 -maxdepth 0 -type f | sort -n | tail -n1 || true)
  if [[ -z "$LATEST" ]]; then
    print_red "Warning: No GCC $GCC_VER toolchain found for arch $D"
    continue
  fi
  NEWLINK=../../../../../$LATEST
  CURLINK=$(readlink etc/toolchain/modulefiles/"$D"/Toolchain/GCC-"${GCC_VER}"||true)
  [[ $CURLINK == $NEWLINK ]] && { echo "No change"; } \
                             || { mkdir -p etc/toolchain/modulefiles/"$D"/Toolchain;
                                  ln -nfsv "$NEWLINK" etc/toolchain/modulefiles/"$D"/Toolchain/GCC-"${GCC_VER}"; }
  echo
done
print_green "All ok"
