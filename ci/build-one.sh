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
# It began as a separate copy of this sequence, which continuous-builder.sh
# kept inline, so the production builders were untouched while the claim path
# was proven. That script is gone; what they shared was build-loop.sh itself,
# which it calls unmodified. That is deliberate -- the build is the part worth
# keeping identical between the sharded and claimed worlds, so that a
# difference in behaviour can never be blamed on two diverging build paths.

. build-helpers.sh

env_name=${1:?usage: build-one.sh ENV_NAME BUILD_TYPE PR_NUMBER PR_HASH [WAITING_SINCE]}
export BUILD_TYPE=${2:?} PR_NUMBER=${3:?} PR_HASH=${4:?} WAITING_SINCE=${5:-}

# Re-read the check's status now that we hold the claim. The claim stops two
# workers building a check at once, but not one after the other: it is released
# as soon as the holder has posted its verdict, and a worker whose listing
# predates that verdict wins the free claim and builds the check all over again.
# While we hold the claim nobody else posts to it, so what we read is final.
# This runs before the per-check venv below, i.e. with the allocation's ali-bot.
# Anything unreadable or unknown means build, as before.
if source_env_files "$env_name"; then
  if ! set-github-status --help 2>/dev/null | grep -q -- --print-state; then
    echo "build-one: set-github-status has no --print-state, building without" \
         "re-reading the status; upgrade the allocation's ali-bot" >&2
  else
    read -r _state _created _id _pr < <(short_timeout set-github-status \
      --print-state -c "$PR_REPO@$PR_HASH" --context "$CHECK_NAME" --pr "$PR_NUMBER")
    _skip=$(stale_listing "$BUILD_TYPE" "$WAITING_SINCE" "$_state" "$_created" "$_pr")
    echo "build-one: $CHECK_NAME on $PR_REPO@$PR_HASH: listed $BUILD_TYPE" \
         "since=${WAITING_SINCE:--}, now ${_state:-unknown} created=${_created:--}" \
         "id=${_id:--} pr=${_pr:--}: ${_skip:-build}" >&2
    # Not $BUILD_MARKER: claim-builder.sh must not count this as a build.
    if [ -n "$_skip" ]; then
      [ -n "$BUILD_MARKER" ] && echo "${_skip%%:*}" > "$BUILD_MARKER.skipped"
      exit 0
    fi
  fi
  unset _state _created _id _pr _skip
fi

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

# One release a week is kept for good. Release builders upload with the default
# --storage ephemeral, so their CAS blobs expire 90 days after they were first
# written -- and a package that rarely changes is deduplicated rather than
# re-uploaded, so its blob keeps that first date however often it is used. Set
# PERMANENT_STORAGE_WEEKDAY (ISO: 1 = Monday ... 7 = Sunday) in a release check's
# *.env and the build of the tag dated on that weekday runs with
# --storage permanent: its own blobs never expire, and every ephemeral blob it
# reuses is promoted, so the whole closure of that week's release is pinned.
#
# Decided from the date IN THE TAG, not from today: every worker, and any
# rebuild of the same tag, makes the same choice. Set here rather than in the
# *.env, which is sourced before the tag is parsed. Reset first, because this
# value must never come from the environment this worker was started with.
ALIBUILD_STORAGE=
if [ -n "$PERMANENT_STORAGE_WEEKDAY" ] && ! is_numeric "$PR_NUMBER"; then
  _weekday=$(python3 -c '
import datetime, re, sys
m = re.search(r"(?:^|-)([0-9]{8})(?=-|_|$)", sys.argv[1])
if not m:
    sys.exit(1)
print(datetime.datetime.strptime(m.group(1), "%Y%m%d").isoweekday())
' "$PR_NUMBER") || _weekday=
  if [ -z "$_weekday" ]; then
    echo "$env_name: no YYYYMMDD date in tag $PR_NUMBER, building it ephemeral" >&2
  elif [ "$_weekday" = "$PERMANENT_STORAGE_WEEKDAY" ]; then
    ALIBUILD_STORAGE=permanent
    echo "$env_name: $PR_NUMBER is dated on weekday $_weekday, building it with --storage permanent" >&2
  fi
  unset _weekday
fi

# A candidate ali-bot under test wins over the *.env pin. Applied HERE, after
# the env files, rather than by making repo-config/DEFAULTS.env respect a
# pre-set INSTALL_ALIBOT: that file is read in a long-lived shell that exports
# these and serves several checks in turn, so a
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
# NOT role-qualified, deliberately. Two roles can name their container the same
# ("slc9" under both mesosci and mesosdaq), so their work areas share this tree.
# That is safe as long as no *.env name is used by both -- each check still gets
# its own <container>/<env_name>. Adding a role prefix here would be more
# obviously correct, but it renames every work area on every claim worker at
# once, buying a fleet-wide wave of cold builds. If you do add one, expect that.
mkdir -p "${CUR_CONTAINER:?}/$env_name"
cd "$CUR_CONTAINER/$env_name" || exit 10

# At the versions this check pins, which is how slc10 gets aliBuild 2.0 while
# every other check stays on the release in repo-config/DEFAULTS.env.
#
# Into a venv PER CHECK, not the allocation's. pip installs scripts by name, so
# two checks pinning different tools overwrite each other's venv/bin/aliBuild:
# bits #114 added aliBuild to its script-files and every later check on the
# worker silently ran bits instead. The allocation venv keeps its own alibuild
# for cleanup.py -- untouched here, and nothing is deleted from either.
tool_pin="${INSTALL_ALIBOT:?} ${INSTALL_ALIBUILD:?}"
[ "$(cat .toolpin 2>/dev/null)" = "$tool_pin" ] || rm -rf .venv
cold=
if [ ! -x .venv/bin/python3 ]; then
  # rm -rf FIRST. `python3 -m venv` NEVER replaces an existing bin/python3, so
  # it cannot repair a half-present venv, and what it does instead depends on
  # the interpreter -- measured 2026-09-26:
  #
  #   python 3.9.18 (EL9, alieevee-wn-8): fails, "Error: [Errno 2] No such file
  #     or directory: '.../.venv/bin/python3'", naming the file it was about to
  #     create. Reads like a missing interpreter; means "this directory is
  #     unusable and I will not touch it".
  #   python 3.14: exits 0 and leaves the dangling symlink in place. The venv
  #     LOOKS created, the interpreter does not run, and the failure surfaces
  #     later in pip. Quieter and worse.
  #
  # So do not test this on a modern python and conclude there is no bug.
  #
  # The rm above only fires on a .toolpin MISMATCH, so a venv that goes missing
  # while its marker still matches lands here for ever.
  #
  # Seen on alieevee-wn-8 on 2026-09-26: every round failed in under a second,
  # which the fast-fail logic correctly cooled -- but the worker then spun at
  # ~4s per round claiming and dropping PRs, which is worse than idling and is
  # the likeliest reason the fleet's shared GraphQL budget went to zero.
  #
  # WHY IT WENT MISSING, and why only there: the host runs Python 3.9.18, where
  # sys._base_executable is the ALLOCATION venv's python rather than the
  # system one, so this venv is built from that venv and holds a reference into
  # it. The allocation venv is deleted and recreated on every task restart
  # ("rm -rf venv" in the entrypoint), which leaves this one derived from
  # something that no longer exists. Python 3.12 nodes (alieevee-wn-9) resolve
  # _base_executable to the real interpreter and never see this.
  rm -rf .venv
  python3 -m venv .venv || exit 1
  general_timeout 900 .venv/bin/python3 -m pip install --upgrade pip || exit 1
  cold=1
fi
# First on PATH, so `python3 -m pip` below installs HERE and build-loop.sh
# resolves aliBuild/bits and the ali-bot CLIs to this check's pins.
export PATH="$PWD/.venv/bin:$PATH"

# $TIMEOUT is as low as 120s on some checks: fine to upgrade an existing venv,
# not enough to populate a fresh one.
pip_timeout=short_timeout
[ -n "$cold" ] && pip_timeout="general_timeout 900"
$pip_timeout python3 -m pip install --upgrade --no-binary=ali-bot \
    "ali-bot[ci] @ git+https://github.com/${INSTALL_ALIBOT:?}"     \
    "git+https://github.com/${INSTALL_ALIBUILD:?}" || exit 1
printf '%s\n' "$tool_pin" > .toolpin

# The build itself, shared verbatim with the production builders.
. build-loop.sh
