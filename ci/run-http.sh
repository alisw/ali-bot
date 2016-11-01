#!/bin/bash -xe

# Parameters:
#
#   ALICE_GH_API  URL to the ALICE GH users mapping API
#   CI_ADMINS     Comma-separated GH usernames of admins
#   DRY_RUN       If set to anything, enable dry run
#   GITLAB_TOKEN  CERN Gitlab token
#   PR_TOKEN      GitHub token for bot user "alibuild"
#   SLEEP         Seconds to sleep between consecutive groups/users updates

set -o pipefail
PROG_DIR="$(dirname "$0")"
PROG_DIR="$(cd "$PROG_DIR"; pwd)"
[[ -d $MESOS_SANDBOX ]] && cd "$MESOS_SANDBOX" || true

# Setup GitLab credentials (to push new data)
printf "protocol=https\nhost=gitlab.cern.ch\nusername=alibuild\npassword=$GITLAB_TOKEN\n" |
  git credential-store --file $PWD/git-creds store
git config --global credential.helper "store --file $PWD/git-creds"

# Setup GitHub API credentials (to communicate with PRs)
echo $PR_TOKEN > $HOME/.github-token

# Clone configuration under "conf"
[[ -d conf/.git ]] || { git clone https://gitlab.cern.ch/ALICEDevOps/ali-marathon.git conf/;
                        pushd conf;
                          git config user.name "ALICE bot";
                          git config user.email "ali.bot@cern.ch";
                        popd; }

# Link configuration into program dir
CONF_DIR="$PWD/conf"
for X in $CONF_DIR/ci_conf/*; do
  ln -nfs $X $PROG_DIR
done

while [[ 1 ]]; do {
  # Continuous ops: update
  pushd conf
    git fetch --all
    git reset --hard origin/HEAD
    git clean -fxd
    pushd ci_conf
      # Errors in both operations are not fatal
      $PROG_DIR/sync-egroups.py > groups.yml0 && mv groups.yml0 groups.yml || { rm -f groups.yml; git checkout groups.yml; }
      $PROG_DIR/sync-mapusers.py "$ALICE_GH_API" > mapusers.yml0 && mv -vf mapusers.yml0 mapusers.yml \
                                                                        || rm -f mapusers.yml0
      git commit -a -m "CI e-groups/users mapping updated" || true
      git push
    popd
  popd
  sleep $SLEEP
} &>> update.log ; done &

cd $PROG_DIR
./process-pull-request-http.py --bot-user alibuild                                                \
                               --admins $CI_ADMINS                                                \
                               ${DRY_RUN:+--dry-run}                                              \
                               ${PROCESS_QUEUE_EVERY:+--process-queue-every $PROCESS_QUEUE_EVERY} \
                               ${PROCESS_ALL_EVERY:+--process-all-every $PROCESS_ALL_EVERY}       \
                               --debug
