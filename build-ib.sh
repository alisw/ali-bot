#!/bin/bash -ex

#
# Build a given version of a software handled by GAR.
#
# At the end of the process packages will be stored as tarballs inside two
# directories, $RegisterTarballsDir and $AllTarballsDir. We are interested in
# the packages created in the latter.
#

function PrintUsage() (
  cat <<'EoF'
Usage:
  build-ib.sh.sh --software <[software_group:]software_name> \
                 --recipe-version <recipe_version> \
                 [--recipe-base-url <base_svn_url>] \
                 [--recipe-svn-user <recipe_svn_user>] \
                 [--recipe-svn-pass <recipe_svn_pass>] \
                 [--ncores <make_j_cores>] \
                 [--drop-to-shell]

Example:
  build-ib.sh.sh --software root --recipe-version v5-06-25

Alternatively it possible to specify parameters as envvars:

  --software        --> DEFAULT_RECIPE_SW
  --recipe-version  --> DEFAULT_RECIPE_VER
  --recipe-base-url --> DEFAULT_RECIPE_BASE_URL
  --recipe-svn-user --> DEFAULT_RECIPE_USER
  --recipe-svn-pass --> DEFAULT_RECIPE_PASS
  --ncores          --> DEFAULT_NCORES

Software name can take two different forms:

  sw_group:sw_name  # e.g. geant4:geant4_vmc

or simply:

  sw_name  # e.g. root

The latter will assume that software group is equal to software name:

  sw_name:sw_name

EoF
)

cd /

# Get variables from the environment
RecipeSw="${DEFAULT_RECIPE_SW:-}"
RecipeVer="${DEFAULT_RECIPE_VER:-}"
MakeCores=${DEFAULT_NCORES:-$( echo 'scale=2;a='`grep -c bogomips /proc/cpuinfo`'*1.05;scale=0;a/1' | bc )}
RecipeBaseUrl=${DEFAULT_RECIPE_BASE_URL:-'http://svn.cern.ch/guest/aliroot-bits/branches'}
RecipeSvnUser="${DEFAULT_RECIPE_USER:-}"
RecipeSvnPass="${DEFAULT_RECIPE_PASS:-}"

while [[ $# -gt 0 ]] ; do
  case "$1" in
    --software) RecipeSw="$2" ; shift 2 ;;
    --recipe-version) RecipeVer="$2" ; shift 2 ;;
    --recipe-base-url) RecipeBaseUrl="$2" ; shift 2 ;;
    --ncores) MakeCores="$2" ; shift 2 ;;
    --recipe-svn-user) RecipeSvnUser="$2" ; shift 2 ;;
    --recipe-svn-pass) RecipeSvnPass="$2" ; shift 2 ;;
    --drop-to-shell) DropToShell=1 ; shift ;;
    *) shift ;;
  esac
done

# Mandatory input variables
if [[ "$RecipeSw" == '' || "$RecipeVer" == '' ]] ; then
  PrintUsage
  exit 1
fi

# Separate group:component with ':'. If not provided, group and component will
# be identical
RecipeSwGroup=${RecipeSw%%:*}
RecipeSwComponent=${RecipeSw#*:}

# Other variables
RecipeUrl="${RecipeBaseUrl}/${RecipeVer}"
RecipeDir='/root/recipe'
BuildScratchDir='/root/scratch'
WwwDir='/opt/aliroot/www'
AltWwwDir='/root/www'
AllTarballsDir="${WwwDir}/tarballs"  # *all* tarballs created
RegisterTarballsDir='/packages_spool'  # only tarballs to be registered

# Start from a clean slate (do not touch tarball dirs)
rm -rf "$BuildScratchDir" "$RecipeDir" "$AltTarballsDir"

# Directories expected by the build system
mkdir -p "$RegisterTarballsDir" "$AllTarballsDir"

# Work around hardcoded paths in recipes
ln -nfs "$WwwDir" "$AltWwwDir"

# Download recipe
if [[ "$RecipeSvnUser" != '' && "$RecipeSvnPass" != '' ]] ; then
  svn checkout "$RecipeUrl" "$RecipeDir" \
    --non-interactive \
    --username "$RecipeSvnUser" --password "$RecipeSvnPass"
else
  svn checkout "$RecipeUrl" "$RecipeDir" --non-interactive
fi

# Configure GAR, the build recipes handler
cd "$RecipeDir"
./bootstrap
./configure --prefix="$BuildScratchDir"

# Build a specific software on all possible cores (override hardcoded values)
cd "${RecipeDir}/apps/${RecipeSwGroup}/${RecipeSwComponent}"

# Drop to shell, or proceed automatically?
if [[ "$DropToShell" == 1 ]] ; then
  exec bash
else
  exec time make -j"$MakeCores" install AUTOREGISTER=0 BUILD_ARGS=-j"$MakeCores"
fi
