# shellcheck shell=bash
# Claim helpers, for workers that take PRs from a shared queue rather than from
# a hash shard. Source this; it defines functions and runs nothing.
#
# Deliberately NOT part of build-helpers.sh. Production builders source that on
# every iteration, and nothing in the sharded stack needs claims yet -- keeping
# them here means the production path is untouched until a job explicitly opts
# in by sourcing this file. See ci/SCALING_PLAN.md, Phase 1.
#
#   . claims.sh
#   if with_claim "$CHECK_NAME" "$PR_HASH" bash "$NOMAD_TASK_DIR/build-one.sh"; then
#     : we held the claim and the build ran
#   else
#     : somebody else holds it, or Nomad was unreachable -- try the next PR
#   fi
#
# A thin wrapper around `nomad var lock`, which is the purpose-built primitive:
# it acquires the lock, runs the child while holding it, RENEWS IN THE
# BACKGROUND for as long as the child runs, and releases when the child exits.
# That last part is why the lease can be minutes while a build takes hours --
# and why there is no heartbeat here to write, tune, or get wrong.
#
# Two earlier drafts did it by hand and were both worse. The first compared
# expiry timestamps client-side, so it depended on every builder's clock
# agreeing and had no lock delay. The second spoke the lock HTTP API directly,
# which needed a background renewal loop and left a window where the build
# carried on after losing its claim. Nomad ships the whole thing; use it.
#
# THE ONE INTEGRATION CONSEQUENCE: the build has to be a *command*, because the
# lock runs it as a child. `. build-loop.sh` sourced into the current shell
# cannot be the child, so the per-PR body wants to live in a small script that
# the lock invokes. Everything build-loop.sh needs is already exported by
# source_env_files, so a child inherits it.
#
# NOMAD_ADDR must point at the task API socket,
# unix://${NOMAD_SECRETS_DIR}/api.sock: the agent does not listen on loopback
# on these nodes, so the CLI's default of 127.0.0.1:4646 is refused. The CLI
# does accept a unix:// address.
#
# Requires the `nomad` binary (present on every builder), NOMAD_ADDR, a
# workload identity in NOMAD_TOKEN (`identity { env = true }`), and an ACL
# policy granting variables:write on $CLAIM_PREFIX -- the automatic workload
# policy covers only nomad/jobs/<this job>, and claims are shared across jobs
# by design, which is what lets a merged pool work.

# A prefix no job owns by default, precisely so the policy granting write on it
# has to be explicit and shared.
: "${CLAIM_PREFIX:=ci/claims}"

# Nomad allows 10s..24h. The TTL bounds how long a dead worker's PR stays stuck
# before another worker may take it: the lock command renews well inside this,
# so it need not have anything to do with how long a build takes.
: "${CLAIM_TTL:=5m}"

# How long the variable stays unlockable after a lease lapses. Guards against a
# holder that was partitioned and still believes it is building.
: "${CLAIM_DELAY:=30s}"

function claim_path () {
  # claim_path CHECK SHA
  # Per (check, commit): the same commit built for two different checks is two
  # different pieces of work. Slashes in check names would otherwise create
  # surprise nesting under the prefix.
  echo "$CLAIM_PREFIX/${1//\//_}/$2"
}

function with_claim () {
  # with_claim CHECK SHA COMMAND [ARGS...]
  # Runs COMMAND while holding the claim, and returns its exit status. Returns
  # non-zero without running anything if somebody else holds the claim.
  #
  # -early-return is what makes this a claim rather than a queue: by default the
  # command waits on standby for the lock to free, so a worker would block on a
  # PR another worker is already building instead of moving to the next one.
  #
  # -shell=false so the child is exec'd directly. With a shell, whether the
  # child gets killed when the lock is lost depends on which shell: dash, which
  # is /bin/sh on Debian and Ubuntu, does not signal its children.
  local check=$1 sha=$2
  shift 2
  nomad var lock -early-return -shell=false \
                 -ttl="$CLAIM_TTL" -delay="$CLAIM_DELAY" \
                 "$(claim_path "$check" "$sha")" "$@"
}
