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
export INSTALL_ALIBUILD INSTALL_ALIBOT INSTALL_ALIDIST

# A work area per check, inside whatever directory we were started in. The
# caller owns that: for a long-lived worker it is the allocation's sticky disk,
# which is what keeps sw/ and the checkouts warm between builds.
mkdir -p "$env_name"
cd "$env_name" || exit 10

# At the versions this check pins, which is how slc10 gets aliBuild 2.0 while
# every other check stays on the release in repo-config/DEFAULTS.env.
short_timeout python3 -m pip install --upgrade --no-binary=ali-bot \
    "ali-bot[ci] @ git+https://github.com/${INSTALL_ALIBOT:?}"     \
    "git+https://github.com/${INSTALL_ALIBUILD:?}" || exit 1

# The build itself, shared verbatim with the production builders.
. build-loop.sh
