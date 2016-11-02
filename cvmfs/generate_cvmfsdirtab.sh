#!/bin/bash -e
cd "$(dirname "$0")/.."
for D in el* ubuntu*; do
  [[ -d "$D" ]] || continue
  echo "/$D/Modules/*"
  find "$D/Packages" -maxdepth 1 -type d -exec echo '/{}/'* \;
done > .cvmfsdirtab
echo All ok
