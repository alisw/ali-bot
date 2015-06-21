#!/bin/bash

#
# Run a Docker builder and call the GAR builder
#

cd "$(dirname "$0")"

DirsPrefix="${HOME}/container-dirs"

while [[ $# -gt 0 ]] ; do
  case "$1" in
    --) shift ; break ;;
    --container) Container="$2" ; shift 2 ;;
    --prefix) DirsPrefix="$2" ; shift 2 ;;
    *) echo "Unknown parameter: $1" ; exit 1 ;;
  esac
done

if [[ "$Container" == '' ]] ; then
  echo "Specify a container with --container"
  exit 1
fi

ContainerFull="alisw/${Container}:latest"

# Output packages will be stored here
PackagesDirHost="${DirsPrefix}/${Container}/packages"
PackagesDirGuest='/packages_spool'

# ccache directory
CcacheDirHost="${DirsPrefix}/${Container}/ccache"
CcacheDirGuest='/ccache'

# Use latest version of the build script
BuilderHost="${PWD}/build-ib.sh"
BuilderGuest='/build-ib.sh'

# Optionally pass SVN credentials to the container
RecipeSvnCreds="$( cat "${PWD}/recipe-svn-creds.txt" 2> /dev/null )"
RecipeSvnUser=${RecipeSvnCreds%%:*}
RecipeSvnPassword=${RecipeSvnCreds#*:}

# Create external directories
mkdir -p "$PackagesDirHost" "$CcacheDirHost"

exec time docker run -it --rm \
  -v "${BuilderHost}:${BuilderGuest}:ro" \
  -v "${PackagesDirHost}:${PackagesDirGuest}:rw" \
  -v "${CcacheDirHost}:${CcacheDirGuest}:rw" \
  -e 'PS1=`[ $? == 0 ] || echo $?\|`'$Container' ~ \u@\h \w \$> ' \
  -e "CCACHE_DIR=${CcacheDirGuest}" \
  -e "HOME=/root" \
  -e "DEFAULT_RECIPE_USER=${RecipeSvnUser}" \
  -e "DEFAULT_RECIPE_PASS=${RecipeSvnPassword}" \
  "$ContainerFull" \
  "$BuilderGuest" "$@"
