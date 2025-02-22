#!/bin/bash -e
set -o pipefail
GCC_VER=${1:-"v13.2.0"}

cd /cvmfs/alice.cern.ch

for D in el* ubuntu*; do
  [[ -d "$D" ]] || continue
  printf "\033[1;34m==>\033[m \033[1mUpdating latest GCC %s for arch %s\033[m\n" "$GCC_VER" "$D"
  LATEST=$(find "$D"/Modules/modulefiles/GCC-Toolchain/*"${GCC_VER}"* -mindepth 0 -maxdepth 0 -type f 2>/dev/null | sort -n | tail -n1 || true)
  if [[ -z "$LATEST" ]]; then
    echo "No GCC toolchain $GCC_VER found for arch $D"
    continue
  fi
  NEWLINK=../../../../../$LATEST
  CURLINK=$(readlink etc/toolchain/modulefiles/"$D"/Toolchain/GCC-"${GCC_VER}"||true)
  if [[ $CURLINK == "$NEWLINK" ]]; then
    printf "\033[1;32mNo change\033[m"
  else
    mkdir -p etc/toolchain/modulefiles/"$D"/Toolchain
    ln -nfsv "$NEWLINK" etc/toolchain/modulefiles/"$D"/Toolchain/GCC-"${GCC_VER}"
  fi
  echo
done
printf "\033[1;32mAll ok\033[m\n"
