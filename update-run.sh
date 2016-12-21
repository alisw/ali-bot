#!/bin/bash -x
RUN_WORKDIR=$RUN_WORKDIR     # relative to the repo's root
RUN_SLEEP=${RUN_SLEEP:-120}  # in seconds
RUN_REPO=$RUN_REPO           # gh_user/gh_repo[:branch]
RUN_REPO_ONLY=${RUN_REPO%:*}
RUN_BRANCH=${RUN_REPO##*:}
[[ $RUN_BRANCH == $RUN_REPO_ONLY ]] && RUN_BRANCH=
[[ -d gitrepo/.git ]] || git clone https://github.com/$RUN_REPO_ONLY ${RUN_BRANCH:+-b $RUN_BRANCH} gitrepo/
pushd gitrepo
  git fetch --all
  git reset --hard origin/$([[ "$RUN_BRANCH" ]] && echo "$RUN_BRANCH" || echo HEAD)
  git clean -fxd
  ls -l
  pushd "$RUN_WORKDIR"
    "$@"
    echo "==> Returned $?"
  popd
popd
sleep $RUN_SLEEP
exec "$0" "$@"
