#!/bin/bash -xe

# Parameters:
#
#   ALICE_GH_API         URL to the ALICE GH users mapping API
#   CI_ADMINS            Comma-separated GH usernames of admins
#   DRY_RUN              If set to anything, enable dry run
#   GITLAB_TOKEN         CERN Gitlab token
#   PROCESS_ALL_EVERY    How often to check PRs (catch up with lost callbacks)
#   PROCESS_QUEUE_EVERY  How often the PR queue is processed
#   PR_TOKEN             GitHub token for bot user "alibuild"
#   SLEEP                Seconds to sleep between consecutive groups/users updates

set -o pipefail
ALI_BOT_DIR="$(dirname "$0")"
ALI_BOT_DIR="$(cd "$ALI_BOT_DIR/.."; pwd)"

# Move into the scratch directory
cd "$NOMAD_TASK_DIR" || :

# Install with pip
export PYTHONUSERBASE=$PWD/python_local
export PATH="$PYTHONUSERBASE/bin:$PATH"
pip3 install --user --upgrade pip
pip3 install --ignore-installed --upgrade --user -e "${ALI_BOT_DIR}[services]"

# Setup GitLab credentials (to push new data)
printf 'protocol=https\nhost=gitlab.cern.ch\nusername=%s\npassword=%s\n' "$GITLAB_USER" "$GITLAB_PASS" |
git credential-store --file ~/.git-creds store
git config --global credential.helper 'store --file ~/.git-creds'

# Setup GitHub API credentials (to communicate with PRs)
echo "$PR_TOKEN" > ~/.github-token

# Clone configuration under "conf"
if [[ ! -d conf/.git ]]; then
  git clone https://gitlab.cern.ch/ALICEDevOps/ali-marathon.git conf/
  pushd conf
    git config user.name "ALICE bot"
    git config user.email "ali.bot@cern.ch"
  popd
fi
pushd conf
  git reset --hard origin/HEAD
  git clean -fxd
popd

# Link configuration here (current dir == program dir)
for X in "$PWD"/conf/ci_conf/*; do
  ln -nfs "$X" .
done

while true; do {
  # Continuous configuration update. Operations here are not fatal
  TIMEOUT_CMD="timeout -s 9 100"
  set +e
  printf '%s: start egroups and conf sync\n' "$(date --iso-8601=seconds)"
  pushd conf
    $TIMEOUT_CMD git fetch --all
    git reset --hard origin/HEAD
    git clean -fxd
    pushd ci_conf
      if $TIMEOUT_CMD sync-egroups.py > groups.yml0; then
        mv groups.yml0 groups.yml
      else
        rm -f groups.yml
        git checkout groups.yml
      fi
      if $TIMEOUT_CMD sync-mapusers.py "$ALICE_GH_API" > mapusers.yml0; then
        mv -vf mapusers.yml0 mapusers.yml
      else
        rm -f mapusers.yml0
      fi
      git commit -a -m "CI e-groups/users mapping updated"
      $TIMEOUT_CMD git push
    popd
  popd
  sleep "$SLEEP"
  set -e
} &>> update.log ; done &

process-pull-request-http.py --bot-user alibuild                                                \
                             --admins "$CI_ADMINS"                                              \
                             ${DRY_RUN:+--dry-run}                                              \
                             ${PROCESS_QUEUE_EVERY:+--process-queue-every $PROCESS_QUEUE_EVERY} \
                             ${PROCESS_ALL_EVERY:+--process-all-every $PROCESS_ALL_EVERY}       \
                             --debug
