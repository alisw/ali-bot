#!/bin/bash -ex

#
# Build a given version of a software handled by GAR.
#
#
# At the end of the process packages will be stored as tarballs inside two
# directories, $RegisterTarballsDir and $FinalTarballsDir.
#

function usage() {
cat << \EOF
 Usage:
   wrap-gar-build.sh --software <software_name> \
                     --recipe-version <recipe_version>

 Example:
   wrap-gar-build.sh --software root --recipe-version v5-06-25
EOF
}

cd /

RecipeSw="${DEFAULT_RECIPE_SW:-}"
RecipeVer="${DEFAULT_RECIPE_VER:-}"

while [[ $# -gt 0 ]] ; do
  case "$1" in
    --software) RecipeSw="$2" ; shift 2 ;;
    --recipe-version) RecipeVer="$2" ; shift 2 ;;
    *) shift ;;
  esac
done

# Mandatory input variables
if [[ "$RecipeSw" == '' ]] ; then
  usage && exit 1
fi

if [[ "$RecipeVer" == '' ]] ; then
  usage && exit 1
fi

# Other variables
RecipeUrl="http://svn.cern.ch/guest/aliroot-bits/branches/${RecipeVer}"
RecipeDir='/root/recipe'
BuildScratchDir='/root/scratch'
WwwDir='/opt/aliroot/www'
AltWwwDir='/root/www'
FinalTarballsDir="${WwwDir}/tarballs"  # *all* tarballs created
RegisterTarballsDir='/packages_spool'  # only tarballs to be registered
MakeCores=$( echo 'scale=2;a='`grep -c bogomips /proc/cpuinfo`'*1.05;scale=0;a/1' | bc )

# Start from a clean slate (do not touch tarball dirs)
rm -rf "$BuildScratchDir" "$RecipeDir" "$AltTarballsDir"

# Directories expected by the build system
mkdir -p "$RegisterTarballsDir" "$FinalTarballsDir"

# Work around hardcoded paths in recipes
ln -nfs "$WwwDir" "$AltWwwDir"

# Download recipe
svn checkout "$RecipeUrl" "$RecipeDir" \
  --non-interactive

# Configure GAR, the build recipes handler
cd "$RecipeDir"
./bootstrap
./configure --prefix="$BuildScratchDir"

# Build a specific software on all possible cores (override hardcoded values)
cd "${RecipeDir}/apps/${RecipeSw}/${RecipeSw}"
exec time make -j"$MakeCores" install AUTOREGISTER=0 BUILD_ARGS=-j"$MakeCores"
