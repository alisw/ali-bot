#!/bin/bash -x
# -*- sh-basic-offset: 2 -*-
# Build exactly one pull request, then exit.
#
#   build-one.sh ENV_NAME BUILD_TYPE PR_NUMBER PR_HASH [WAITING_SINCE]
#
# This exists because a claim runs its holder as a CHILD PROCESS: `nomad var
# lock` acquires the lock, execs a command, renews while it runs, and releases
# when it exits. `. build-loop.sh` sourced into a loop's shell cannot be that
# child, so the per-PR body has to be a command. See ci/claims.sh.
#
# It does NOT replace continuous-builder.sh, which keeps its own inline copy of
# this sequence. The production builders are untouched by anything here: this
# file is new, and the only thing it shares with them is build-loop.sh itself,
# which it calls unmodified. That is deliberate -- the build is the part worth
# keeping identical between the sharded and claimed worlds, so that a
# difference in behaviour can never be blamed on two diverging build paths.

. build-helpers.sh

env_name=${1:?usage: build-one.sh ENV_NAME BUILD_TYPE PR_NUMBER PR_HASH [WAITING_SINCE]}
export BUILD_TYPE=${2:?} PR_NUMBER=${3:?} PR_HASH=${4:?} WAITING_SINCE=${5:-}

# Tell the caller we actually ran, as opposed to never being started because
# somebody else held the claim. The two are indistinguishable from the exit
# status alone: `nomad var lock` returns the child's status when it runs one,
# and its own when it does not.
[ -n "$BUILD_MARKER" ] && : > "$BUILD_MARKER"

# build-loop.sh reports "Queued (N ahead)" for the rest of $HASHES. With claims
# there is no such thing: this worker does not own the tail of any list, and
# another worker may be building the next entry already. Leaving HASHES unset
# makes that loop a no-op. SCALING_PLAN.md Phase 3 has the queued statuses
# moving to the lister, which does have the global view.
export BUILD_SEQ=1
unset HASHES

# Skip quietly if the check disappeared from repo-config between the listing
# and now -- it is not an error, there is simply nothing to build.
source_env_files "$env_name" || exit 0

# A release tag carries its own metadata -- which suite, which day, which cut,
# whether it is a test one -- and TAG_REGEX, the SAME regex list-release-tags
# matched it with, pulls it back out: every named group becomes TAG_<NAME> in
# the environment. For
#
#   TAG_REGEX='^(?P<suite>[^-]+)-daily-%(date)-(?P<time>[0-9]{4})(?P<variant>_.+)?$'
#
# the tag O2PDPSuite-daily-20260825-1100_TEST yields TAG_SUITE=O2PDPSuite,
# TAG_TIME=1100, TAG_VARIANT=_TEST. One regex, so what selects a release and
# what describes it cannot disagree.
#
# The %(date) is expanded over the same window the lister offered, because a
# worker may be building yesterday's tag when RELEASE_LOOKBACK_DAYS allows it.
#
# Unset for pull-request checks, where $PR_NUMBER is a number and there is
# nothing to parse, so this is a no-op for every existing builder.
#
# A regex that matches nothing in the window is FATAL rather than skipped: the
# point is to name what gets published, and publishing a release under values
# guessed because the name was not what we expected is worse than not
# publishing.
if [ -n "$TAG_REGEX" ]; then
  tag_env=
  _day=0
  while [ "$_day" -le "${RELEASE_LOOKBACK_DAYS:-0}" ]; do
    _regex=$(expand_date_spec "$TAG_REGEX" "$_day") || break
    _day=$((_day + 1))
    tag_env=$(python3 -c '
import re, shlex, sys
m = re.match(sys.argv[1], sys.argv[2])
if not m:
    sys.exit(1)
for name, value in (m.groupdict() or {}).items():
    if not name.isidentifier():
        sys.exit("TAG_REGEX group %r is not a usable variable name" % name)
    print("TAG_%s=%s" % (name.upper(), shlex.quote(value or "")))
' "$_regex" "$PR_NUMBER") && break
    tag_env=
  done
  [ -n "$tag_env" ] || {
    echo "$env_name: TAG_REGEX matches no day in the window for tag" \
         "$PR_NUMBER, refusing to build it" >&2
    exit 1
  }
  # eval, but only over shlex.quote()d values whose names we validated above.
  eval "$tag_env"
  # shellcheck disable=SC2046  # deliberate: export the names we just set
  export $(printf '%s\n' "$tag_env" | cut -d= -f1)
  unset _day _regex tag_env
fi

# A candidate ali-bot under test wins over the *.env pin. Applied HERE, after
# the env files, rather than by making repo-config/DEFAULTS.env respect a
# pre-set INSTALL_ALIBOT: that file is read by continuous-builder.sh too, in a
# long-lived shell that exports these and serves several checks in turn, so a
# ${VAR:-default} there would let the first check's pin stick to every later
# one. Overriding in this process, which builds exactly one PR and exits, cannot
# leak anywhere.
[ -n "$ALIBOT_OVERRIDE" ] && INSTALL_ALIBOT=$ALIBOT_OVERRIDE

export INSTALL_ALIBUILD INSTALL_ALIBOT INSTALL_ALIDIST

# A work area per check, inside whatever directory we were started in. The
# caller owns that: for a long-lived worker it is the allocation's sticky disk,
# which is what keeps sw/ and the checkouts warm between builds.
#
# Qualified by container, because the *.env name alone is not unique: o2-alidist
# exists under slc10 and ubuntu2204 both. They share nothing that could safely
# live in one directory -- sw/BUILD/<pkg>-latest and the merged PR checkout are
# not architecture-namespaced, so two containers in one work area would each
# clobber the other's tree and report-pr-errors would upload whichever log won.
mkdir -p "${CUR_CONTAINER:?}/$env_name"
cd "$CUR_CONTAINER/$env_name" || exit 10

# At the versions this check pins, which is how slc10 gets aliBuild 2.0 while
# every other check stays on the release in repo-config/DEFAULTS.env.
short_timeout python3 -m pip install --upgrade --no-binary=ali-bot \
    "ali-bot[ci] @ git+https://github.com/${INSTALL_ALIBOT:?}"     \
    "git+https://github.com/${INSTALL_ALIBUILD:?}" || exit 1

# The build itself, shared verbatim with the production builders.
. build-loop.sh
