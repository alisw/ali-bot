#!/bin/bash -xe
set -o pipefail
[[ -d $MESOS_SANDBOX ]] && cd "$MESOS_SANDBOX" || true
mkdir -p log

# Setup GitLab credentials (to push new data)
printf "protocol=https\nhost=gitlab.cern.ch\nusername=alibuild\npassword=$GITLAB_TOKEN\n" |
  git credential-store --file $PWD/git-creds store
git config --global credential.helper "store --file $PWD/git-creds"

# Setup GitHub API credentials (to communicate with PRs)
echo $PR_TOKEN > $HOME/.github-token

# Clone code under "code"
CI_REPO=$CI_REPO # gh_user/gh_repo[:branch]
CI_REPO_ONLY=${CI_REPO%:*}
CI_BRANCH=${CI_REPO##*:}
git clone https://github.com/$CI_REPO_ONLY ${CI_BRANCH:+-b $CI_BRANCH} code/

# Clone configuration under "conf"
[[ -d conf/.git ]] || { git clone https://gitlab.cern.ch/ALICEDevOps/ali-marathon.git conf/;
                        pushd conf;
                          git config user.name "ALICE bot";
                          git config user.email "ali.bot@cern.ch";
                        popd; }

while [[ 1 ]]; do {
  # Continuous ops: update
  pushd conf
    git fetch --all
    git reset --hard origin/HEAD
    git clean -fxd
    pushd ci_conf
      # Errors in both operations are not fatal
      ../../code/ci/sync-egroups.py > groups.yml || { rm -f groups.yml; git checkout groups.yml; }
      ../../code/ci/sync-mapusers.py "$ALICE_GH_API" > mapusers.yml0 && mv -vf mapusers.yml0 mapusers.yml \
                                                                        || rm -f mapusers.yml0
      git commit -a -m "CI e-groups/users mapping updated" || true
      git push
    popd
  popd
  pushd code
    git fetch --all
    git reset --hard origin/$([[ "$CI_BRANCH" ]] && echo "$CI_BRANCH" || echo HEAD)
    git clean -fxd
    pushd ci
      for X in ../../conf/ci_conf/*; do
        ln -nfs $X .
      done
      ls -l
      ./process-pull-request --admins $CI_ADMINS --bot-user alibuild --debug ${DRY_RUN:+--dry-run}
    popd
  popd

  find log/ -name 'pr-*.log' -type f -mtime +5 -delete || true
  sleep $SLEEP
} &> log/pr-$(date --utc +%Y%m%d-%H%M%S); done
