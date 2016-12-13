#!/bin/bash -e
set -o pipefail
GCC_VER=${1:-"v4.9.3"}
cd "$(dirname "$0")/.."
for D in el* ubuntu*; do
  [[ -d "$D" ]] || continue
  printf "\033[1;34m==>\033[m \033[1mUpdating latest GCC $GCC_VER for arch $D\033[m\n"
  LATEST=$(find $D/Modules/modulefiles/GCC-Toolchain/*${GCC_VER}* -mindepth 0 -maxdepth 0 -type f -printf "%T@ %p\n" | sort -n | tail -n1 | cut -d' ' -f2-)
  NEWLINK=../../../../../$LATEST
  CURLINK=$(readlink etc/toolchain/modulefiles/$D/Toolchain/GCC-${GCC_VER}||true)
  [[ $CURLINK == $NEWLINK ]] && { echo "No change"; } \
                             || { mkdir -p etc/toolchain/modulefiles/$D/Toolchain;
                                  ln -nfsv $NEWLINK etc/toolchain/modulefiles/$D/Toolchain/GCC-${GCC_VER}; }
  echo
done
printf "\033[1;32mAll ok\033[m\n"
