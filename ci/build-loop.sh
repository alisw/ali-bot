#!/bin/bash -x
# This is the inner loop of continuous-builder.sh. This script builds one pull
# request for one repository. Which repo is checked depends on the environment
# variables passed to this script; they are set in continuous-builder.sh from
# environment definitions in repo-config/.
#
# Some functions used here are defined in continuous-builder.sh.

. build-helpers.sh
get_config

ensure_vars CI_NAME CHECK_NAME PR_REPO PR_BRANCH PACKAGE ALIBUILD_DEFAULTS
: "${WORKERS_POOL_SIZE:=1}" "${WORKER_INDEX:=0}" "${PR_REPO_CHECKOUT:=$(basename "$PR_REPO")}"
[ -d /build/mirror ] && : "${MIRROR:=/build/mirror}"

# This is the check name. If CHECK_NAME is in the environment, use it. Otherwise
# default to, e.g., build/AliRoot/release (build/<Package>/<Defaults>)
: "${CHECK_NAME:=build/$PACKAGE/$ALIBUILD_DEFAULTS}"

host_id=$(echo "$MESOS_EXECUTOR_ID" |
            sed -ne 's#^\(thermos-\)\?\([a-z]*\)-\([a-z]*\)-\([a-z0-9_-]*\)-\([0-9]*\)\(-[0-9a-f]*\)\{5\}$#\2/\4/\5#p')
: "${host_id:=$(hostname -f)}"

# Update all PRs in the queue with their number before we start building.
echo "$HASHES" | tail -n "+$((BUILD_SEQ + 1))" | cat -n | while read -r ahead btype num hash envf; do
  # Run this in a subshell as report_pr_errors uses $PR_REPO but we don't want
  # to overwrite the outer for loop's variables, as they are needed for the
  # subsequent build.
  (
    cd ..
    source_env_files "$envf"
    case "$btype" in
      # Create status if we've never tested this before.
      untested)
        PR_NUMBER=$num PR_HASH=$hash report_pr_errors --pending -m "Queued ($ahead ahead) on $host_id" ;;

      # If we've tested this before and it was red, there's an existing status.
      # Keep it, just change the message to say we're rechecking.
      failed)
        set-github-status -k -c "$PR_REPO@$hash" -s "$CHECK_NAME/$(build_type_to_status "$btype")" \
                          -m "Queued for recheck ($ahead ahead) on $host_id" ;;

      # If the previous check was green, we probably still have the build
      # products cached, so the rebuild will be almost instantaneous. Don't
      # update the status to say we're rechecking as that would eat into our
      # API request quota too quickly.
      succeeded) ;;
    esac
  ) < /dev/null  # Stop commands from slurping hashes, just in case.
done

case "$BUILD_TYPE" in
  # Create a status on GitHub showing the build start time, but only if this is
  # the first build of this check!
  untested) report_pr_errors --pending -m "Started $(TZ=Europe/Zurich date +'%a %H:%M CET') @ $host_id" ;;
  # Rebuilds only change the existing status's message, keeping the red status
  # and URL intact.
  failed) set-github-status -k -c "$PR_REPO@$PR_HASH" -s "$CHECK_NAME/$(build_type_to_status "$BUILD_TYPE")" \
                            -m "Rechecking since $(TZ=Europe/Zurich date +'%a %H:%M CET') @ $host_id" ;;
  # See above for why we don't update the status for green checks.
  succeeded) ;;
esac

# A few common environment variables when reporting status to analytics.
# In analytics we use screenviews to indicate different states of the
# processing and events to indicate all the things we would consider as
# fatal in a non deamon process but that here simply make us go to the
# next step.
ALIBOT_ANALYTICS_USER_UUID=$(hostname -s)-$WORKER_INDEX${CI_NAME:+-$CI_NAME}
ALIBOT_ANALYTICS_ARCHITECTURE=${CUR_CONTAINER}_$(uname -m)
export ALIBOT_ANALYTICS_USER_UUID ALIBOT_ANALYTICS_ARCHITECTURE
export ALIBOT_ANALYTICS_APP_NAME=continuous-builder.sh

case $(uname -s) in
  Darwin) use_docker=;;  # We don't run under docker on MacOS.
  *) # Fetch/update needed Docker image, then clean up untagged, unused images.
    docker pull "$CONTAINER_IMAGE"
    docker image prune -f
    use_docker=true;;
esac

# Get dependency development packages
if [ -n "$DEVEL_PKGS" ]; then
  echo "$DEVEL_PKGS" | while read -r gh_url branch checkout_name; do
    reset_git_repository "${checkout_name:-$(basename "$gh_url")}"                  \
                         "https://github.com/$gh_url" ${branch:+--branch "$branch"} \
                         < /dev/null   # Make sure we're not reading DEVEL_PKGS here.
  done
fi

# Remove logs older than 5 days
find separate_logs/ -type f -mtime +5 -delete || true
find separate_logs/ -type d -empty -delete || true

# Run preliminary cleanup command
aliBuild clean --debug

# We are looping over several build hashes here. We will have one log per build.
mkdir -p "separate_logs/$(date -u +%Y%m%d-%H%M%S)-$PR_NUMBER-$PR_HASH"

report_state pr_processing

# Fetch the PR's changes to the git repository.
if pushd "$PR_REPO_CHECKOUT"; then
  git config --add remote.origin.fetch '+refs/pull/*/head:refs/remotes/origin/pr/*'
  # Only fetch destination branch for PRs (for merging), and the PR we are checking now
  short_timeout git fetch origin "+$PR_BRANCH:refs/remotes/origin/$PR_BRANCH"
  if is_numeric "$PR_NUMBER"; then
    short_timeout git fetch origin "+pull/$PR_NUMBER/head"
  fi
  git reset --hard "origin/$PR_BRANCH"  # reset to branch target of PRs
  git clean -fxd
  old_size=$(du -sm . | cut -f1)
  base_hash=$(git rev-parse --verify HEAD)  # reference upstream hash

  if ! git merge --no-edit "$PR_HASH"; then
    # clean up in case the merge fails
    git reset --hard HEAD
    git clean -fxd
    # We do not want to kill the system is github is not working
    # so we ignore the result code for now
    short_timeout set-github-status ${SILENT:+-n} -c "$PR_REPO@$PR_HASH" -s "$CHECK_NAME/error" -m 'Please resolve merge conflicts' ||
      short_timeout report-analytics exception --desc 'set-github-status fail on cannot merge'
    exit 1
  fi

  if [ $(($(du -sm . | cut -f1) - old_size)) -gt "${MAX_DIFF_SIZE:-5}" ]; then
    # We do not want to kill the system is github is not working
    # so we ignore the result code for now
    short_timeout set-github-status ${SILENT:+-n} -c "$PR_REPO@$PR_HASH" -s "$CHECK_NAME/error" -m 'PR too big. Rejecting.' ||
      short_timeout report-analytics exception --desc 'set-github-status fail on merge too big'
    report_pr_errors -m 'Your pull request exceeded the allowed size. If you need to commit large files, [have a look here](http://alisw.github.io/git-advanced/#how-to-use-large-data-files-for-analysis).' ||
      short_timeout report-analytics exception --desc 'report-pr-errors fail on merge diff too big'
    exit 1
  fi

  popd
fi

if ! clean_env short_timeout aliDoctor --defaults "$ALIBUILD_DEFAULTS" "$PACKAGE" \
     ${use_docker:+--architecture "$ARCHITECTURE" --docker-image "$CONTAINER_IMAGE"}
then
  # We do not want to kill the system is github is not working
  # so we ignore the result code for now
  short_timeout set-github-status ${SILENT:+-n} -c "$PR_REPO@$PR_HASH" -s "$CHECK_NAME/error" -m 'aliDoctor error' ||
    short_timeout report-analytics exception --desc 'set-github-status fail on aliDoctor error'
  # If doctor fails, we can move on to the next PR, since we know it will not work.
  # We do not report aliDoctor being ok, because that's really a granted.
  exit 1
fi

# Each round we delete the "latest" symlink, to avoid reporting errors
# from a previous one. In any case they will be recreated if needed when
# we build.
mkdir -p sw/BUILD
find sw/BUILD/ -maxdepth 1 -name '*latest*' -delete
# Delete coverage files from one run to the next to avoid
# reporting them twice under erroneous circumstances
find sw/BUILD/ -maxdepth 4 -name coverage.info -delete

# Only publish packages to remote store when we build the master branch. For
# PRs, PR_NUMBER will be numeric; in that case, only write to the regular
# read-only store. We can't compare against 'master' here as 'dev' is the
# "master branch" for O2.
if ! is_numeric "$PR_NUMBER"; then
  REMOTE_STORE=$BRANCH_REMOTE_STORE
fi

# Ensure build names do not clash across different PR jobs (O2-373)
build_identifier=${NO_ASSUME_CONSISTENT_EXTERNALS:+${PR_NUMBER//-/_}}
: "${build_identifier:=${CHECK_NAME//\//_}}"

# Set up alien.py.
# If we don't find certs in any of these dirs, leave X509_USER_{CERT,KEY}
# unset, but continue. In that case, granting a token will fail, which just
# means that build jobs won't get their jalien_token_{cert,key} variables.
for certdir in /etc/httpd /root/.globus /etc/grid-security ~/.globus; do
  if [ -r "$certdir/hostcert.pem" ] && [ -r "$certdir/hostkey.pem" ]; then
    export X509_USER_CERT=$certdir/hostcert.pem X509_USER_KEY=$certdir/hostkey.pem
    break
  fi
done
# Find CA certs. On alibuilds, the CERN-CA-certs package installs them under
# /etc/pki/tls/certs, but /etc/grid-security is used on other machines.
# If we have CVMFS, it should take priority because on those machines,
# /etc/pki/tls/certs might be an empty directory.
for certdir in /cvmfs/alice.cern.ch/etc/grid-security/certificates \
                 /etc/grid-security/certificates /etc/pki/tls/certs; do
  if [ -d "$certdir" ]; then
    export X509_CERT_DIR=$certdir
    break
  fi
done
# Get a temporary JAliEn token certificate and key, to give anything we build
# below access.
if jalien_token=$(alien.py token -v 1); then
  jalien_token_cert=$(echo "$jalien_token" | sed -n '/^-----BEGIN CERTIFICATE-----$/,/^-----END CERTIFICATE-----$/p')
  jalien_token_key=$(echo "$jalien_token" | sed -n '/^-----BEGIN RSA PRIVATE KEY-----$/,/^-----END RSA PRIVATE KEY-----$/p')
fi
unset certdir jalien_token

# o2checkcode needs the ALIBUILD_{HEAD,BASE}_HASH variables.
# We need "--no-auto-cleanup" so that build logs for dependencies are kept, too.
# For instance, when building O2FullCI, we want to keep the o2checkcode log, as
# report-pr-errors looks for errors in it.
# --docker-extra-args=... uses an equals sign as its arg can start with "--",
# --which would confuse argparse if passed as a separate argument.
if ALIBUILD_HEAD_HASH=$PR_HASH ALIBUILD_BASE_HASH=$base_hash \
     clean_env long_timeout aliBuild build "$PACKAGE"        \
     -j "${JOBS:-$(nproc)}" -z "$build_identifier"           \
     --defaults "$ALIBUILD_DEFAULTS"                         \
     ${MIRROR:+--reference-sources "$MIRROR"}                \
     ${REMOTE_STORE:+--remote-store "$REMOTE_STORE"}         \
     -e "ALIBUILD_O2_TESTS=$ALIBUILD_O2_TESTS"               \
     -e "ALIBUILD_O2PHYSICS_TESTS=$ALIBUILD_O2PHYSICS_TESTS" \
     ${jalien_token_cert:+-e "JALIEN_TOKEN_CERT=$jalien_token_cert"} \
     ${jalien_token_key:+-e "JALIEN_TOKEN_KEY=$jalien_token_key"} \
     ${use_docker:+-e GIT_CONFIG_COUNT=1}                    \
     ${use_docker:+-e GIT_CONFIG_KEY_0=credential.helper}    \
     ${use_docker:+-e GIT_CONFIG_VALUE_0='store --file /.git-creds'} \
     ${use_docker:+-v "$HOME/.git-creds:/.git-creds:ro"}     \
     ${use_docker:+--architecture "$ARCHITECTURE"}           \
     ${use_docker:+--docker-image "$CONTAINER_IMAGE"}        \
     ${use_docker:+--docker-extra-args="$DOCKER_EXTRA_ARGS"} \
     --fetch-repos --debug --no-auto-cleanup
then
  if is_numeric "$PR_NUMBER"; then
    # This is a PR. Use the error function (with --success) to still provide logs
    report_pr_errors --success
  else
    # This is a branch
    short_timeout set-github-status ${SILENT:+-n} -c "$PR_REPO@$PR_HASH" -s "$CHECK_NAME/success"
  fi ||
    short_timeout report-analytics exception --desc 'report-pr-errors fail on build success'
  PR_OK=1
else
  report_pr_errors ${DONT_USE_COMMENTS:+--no-comments} ||
    short_timeout report-analytics exception --desc 'report-pr-errors fail on build error'
  PR_OK=0
fi

# Run post-build cleanup command
aliBuild clean --debug

(
  # Look for any code coverage file for the given commit and push it to
  # codecov.io. Run in a subshell because we might unset PR_NUMBER, which
  # report_state (below) needs.
  coverage_sources=$PWD/$PR_REPO_CHECKOUT
  coverage_info_dir=$(find sw/BUILD/ -maxdepth 4 -name coverage.info -prune -printf %h)
  if [ -n "$coverage_info_dir" ] && cd "$coverage_info_dir"; then
    # If not a number, it's the branch name -- in that case, we don't want to
    # pass -P to codecov.
    is_numeric "$PR_NUMBER" || unset PR_NUMBER
    short_timeout bash <(curl --max-time 600 -s https://codecov.io/bash)  \
                  -R "$coverage_sources" -f coverage.info -C "$PR_HASH"   \
                  ${PR_BRANCH:+-B $PR_BRANCH} ${PR_NUMBER:+-P $PR_NUMBER} ||
      true
  fi
)

report_state pr_processing_done
