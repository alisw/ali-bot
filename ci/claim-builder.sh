#!/bin/bash -x
# -*- sh-basic-offset: 2 -*-
# A build loop that takes work by CLAIMING it, rather than by owning a hash
# shard of it. The claimed counterpart of continuous-builder.sh.
#
# continuous-builder.sh is deliberately left alone: it drives every production
# builder, and the two differ in ways that cannot be expressed as a flag --
# which PRs a worker considers, how it avoids duplicating another worker, and
# whether it re-execs itself. Running them side by side is also what lets a
# single pool be migrated at a time. See ci/SCALING_PLAN.md, Phases 1-3.
#
# What it does each round:
#   1. optionally source $ROUND_SETUP, for credentials that expire;
#   2. refresh the *.env files from ali-bot@master;
#   3. list every buildable PR, in the order the lister recommends;
#   4. walk that list and build the FIRST one it can claim;
#   5. sleep only if it built nothing.
#
# Because every worker asks for the whole list and claims decide who builds
# what, workers need no coordination and no identity: add one and the queue
# drains faster, remove one and its claim lapses. That is the property the hash
# sharding cannot provide.
#
# Environment:
#   MESOS_ROLE, CUR_CONTAINER, ALIBOT_CONFIG_SUFFIX   which pool to serve
#   CUR_CONTAINERS  space-separated containers this worker serves, defaulting
#                  to just $CUR_CONTAINER. One worker can hold the queues of
#                  several platforms at once -- slc10 and ubuntu2204 say -- so
#                  retiring a per-platform builder does not retire its check.
#   GITHUB_TOKEN                                       (or via $ROUND_SETUP)
#   ROUND_SETUP    optional file SOURCED at the start of every round. Sourced,
#                  not run, so it can export credentials into this shell --
#                  which is the point: a gate token from a credential broker
#                  expires, and re-execing would carry a stale one forever.
#   IDLE_SLEEP     seconds to wait after a round that built nothing (300)
#   MAX_IDLE_SLEEP cap for the GitHub-budget backoff below (3600)
#   LISTER_CACHE_TTL   seconds one poll's listings may be shared between
#                      containers (60); 0 disables
#   FAST_FAIL_SECONDS   a failed round shorter than this is treated as an
#                       INFRASTRUCTURE failure rather than a build failure (60).
#                       Not a sharp line: 2026-09-19's infrastructure failures
#                       ran 0-7s against a 30s quickest genuine build failure,
#                       but 2026-09-21's took 45-54s because VecGeom really did
#                       try to build before dying. 60 catches both and will
#                       occasionally catch a real fast failure too -- which
#                       costs one check a 10 minute cooldown on one worker, so
#                       the false positive is much cheaper than the miss.
#   FAST_FAIL_LIMIT     consecutive infrastructure failures of ONE CHECK before
#                       that check is put on cooldown for this worker (3)
#   FAST_FAIL_COOLDOWN  how long that check is skipped, seconds (600)
#
# The ceiling on all this is not a number of checks but the condition it was
# meant to stand for: a pass that took NOTHING and skipped something only
# because it was cooling drops every cooldown, so the next pass considers
# everything. A worker can therefore never idle on its own cooldowns, however
# many checks it serves -- a fixed limit got that wrong in both directions,
# firing at three cooling checks on a worker that still had two to build.
#   CLEANUP_MAX_AGE_DAYS   delete builds older than this, per round (2)
#   CLEANUP_MIN_FREE_GIB   emergency floor: if still below this much free,
#                          keep deleting until it is met (50). See below --
#                          an unreachable value wipes every work area.
#   HEARTBEAT      seconds between "still building X" lines on stdout (300);
#                  0 disables them

. build-helpers.sh
. claims.sh

: "${IDLE_SLEEP:=300}" "${TIMEOUT:=600}" "${LONG_TIMEOUT:=36000}"
# BACKOFF FOR A SHARED BUDGET, not for this worker's own trouble.
#
# Every claim worker in the fleet spends one GitHub GraphQL allowance: 5000
# points an hour, and a listing costs 2 per (repo, branch). A ci-linux-x86
# worker walks 15 of those per poll, so seventeen of them idling on a flat 300s
# interval spend 6120 points an hour -- more than the whole budget.
#
# The trap this fixes is not the overspend, it is the RECOVERY. A rate-limited
# lister returns nothing, which is indistinguishable from "no work", so the loop
# slept its normal 300s and asked again, forever, at exactly the moment the
# budget needed to be left alone. On 2026-09-26 alieevee-wn-8 sat idle for seven
# hours doing that, 42 rate-limit errors deep, while 18 aarch64 PRs stayed red.
#
# So the lister now exits 75 (EX_TEMPFAIL) for "GitHub said wait", and this
# doubles its wait each time it sees that, up to MAX_IDLE_SLEEP. Anything else
# -- including a listing that legitimately found no work -- resets it.
: "${MAX_IDLE_SLEEP:=3600}"
#: Exit status list-branch-pr uses for "the shared budget is spent". Keep in
#: step with EX_TEMPFAIL in list-branch-pr.
readonly LISTER_TEMPFAIL=75
lister_backoff=$IDLE_SLEEP
: "${FAST_FAIL_SECONDS:=60}" "${FAST_FAIL_LIMIT:=3}" "${FAST_FAIL_COOLDOWN:=600}"
# check|consecutive-fast-failures|when, same shape as $attempted above.
fastfail=
: "${HEARTBEAT:=300}"
: "${CUR_CONTAINERS:=$CUR_CONTAINER}"

# An entry in CUR_CONTAINERS is "container" or "role:container". The bare form
# means $MESOS_ROLE, exactly as before; the prefixed form lets ONE worker serve
# several repo-config trees, which is how the mesosdaq queue (10 checks, its own
# tree, container also called "slc9") is served without a job of its own.
#
# The role has to travel with the container because it is not derivable from it:
# repo-config is <role>/<container>/*.env and mesosci and mesosdaq BOTH have an
# slc9 directory holding different checks.
entry_role ()      { case $1 in *:*) printf %s "${1%%:*}" ;; *) printf %s "$MESOS_ROLE" ;; esac; }
entry_container () { printf %s "${1##*:}"; }

# What produces this round's candidate rows. Unquoted on use, so it carries its
# own flags; `-c <container>` is appended per container.
#
# The loop does not care what a "unit of work" is -- it claims a (check, sha)
# and hands it to build-one.sh -- so swapping the lister is enough to serve
# something other than pull requests. list-release-tags does exactly that for
# tagged releases, which is why this is a variable rather than a hardcoded call.
# --cache-ttl: one lister process runs per container and containers share
# repositories, so ci-linux-x86 asks 15 questions a poll for 8 distinct
# (repo, branch) pairs. Well under IDLE_SLEEP, so every poll starts cold and
# only the burst within one poll is shared.
#
# Safe because the claim lock, not the listing, is what stops two workers taking
# the same PR: a stale row costs a lost claim, which this loop already handles.
: "${LISTER_CACHE_TTL:=60}"
: "${LISTER:=list-branch-pr --all-groups --no-status --cache-ttl $LISTER_CACHE_TTL}"

# The same identity continuous-builder.sh sets, for the same reason: the build
# MERGES the PR into the base branch, and git refuses to commit without one --
# "fatal: empty ident name". This lives in the entrypoint rather than in
# build-one.sh because it is per-worker setup, not per-PR.
#
# Left out of the first version of this script, which is what made the loop fail
# in setup on every round: the merge died before any compilation, so no build
# ever ran. Anything else continuous-builder.sh does once at startup belongs
# here too -- it is not a shared prologue, and nothing warns when it diverges.
git config --global user.name alibuild
git config --global user.email alibuild@cern.ch

# The claim key must be globally unique for a piece of work, and *.env names
# are not: o2-alidist exists under several pools. CHECK_NAME is the GitHub
# status context, which is unique by construction. Read it in a subshell so the
# env files cannot leak into the loop.
#
# Everything the loop needs about a check comes back from ONE subshell. Sourcing
# the env files is the expensive part, and it is now needed for ordering as well
# as for the claim, so it is done once per *.env per round rather than once per
# row -- a queue of 30 PRs across 2 checks costs 2 sourcings, not 30.
function check_env_for () (
  # "$container/$env". Everything downstream keys off this pair rather than the
  # bare *.env name, because o2-alidist exists under several containers and is a
  # DIFFERENT check in each -- different architecture, image and work area.
  # shellcheck disable=SC2030  # deliberately local to this subshell
  MESOS_ROLE=$(entry_role "${1%%/*}") CUR_CONTAINER=$(entry_container "${1%%/*}")
  source_env_files "${1#*/}" > /dev/null 2>&1 || exit 0
  # '|' and not a tab: IFS treats runs of WHITESPACE as one delimiter, so a
  # check with no ONLY_PRS collapsed its empty field and shifted REQUIRES_POOL
  # into only_prs -- every GPU row then failed an allowlist it was never subject
  # to, and the worker skipped exactly the work it existed for, silently.
  printf '%s|%s|%s|%s\n' "$CHECK_NAME" "${ONLY_PRS:-}" "${REQUIRES_POOL:-}" \
         "${POOL_PRIORITY:-}"
)

# What this worker has already built, as "$check|$sha" lines.
#
# The loop's only other notion of "done" is the GitHub status the build posts,
# which the lister then sees and stops offering. That breaks down whenever the
# status does not get written -- SILENT mode during a bring-up, or a failing
# report-pr-errors -- and the failure mode is a LIVELOCK, not a slowdown: the
# just-built PR is still untested, still sorts first, and gets rebuilt forever
# while the rest of the queue starves. Observed on slc10: 11 builds of one PR
# in five minutes.
#
# The sharded loop never needed this because random.sample() picked a different
# PR each round, so a missing status cost one wasted rebuild rather than all of
# them. Walking an ordered list is what turns it fatal, and claiming is what
# makes the list ordered.
#
# Keyed by commit, so a new push is a new key and gets built.
#
# Entries EXPIRE, because a permanent block is a bigger hammer than the livelock
# needs. What separates the two cases is time: a missing status re-offers the
# same work within seconds, while a retry worth having -- a transient failure, a
# fixed dependency, an operator clearing a bad cache -- is wanted minutes or
# hours later. A cooldown keeps the first bounded (one rebuild per cooldown per
# worker instead of the 11-in-5-minutes above) and lets the second happen on its
# own.
#
# That matters most at count=1, where this variable IS the fleet's memory and a
# failed build parks the queue until a human restarts the allocation. With
# several workers the others were always free to pick the work up; a single
# worker has nobody to hand it to.
CLAIM_RETRY_COOLDOWN=${CLAIM_RETRY_COOLDOWN:-3600}
attempted=

# ---- what this worker is doing, on stdout -----------------------------------
#
# The build itself writes to stderr, and it writes a LOT: a slc10 round is
# ~230k lines, and Nomad rotates the capture by SIZE (3 files x 200 MB), so any
# line describing what is being built scrolls out of reach long before the build
# ends. Asking "what is this worker on?" then means reading hundreds of
# megabytes to find one line near the start.
#
# So stdout is kept as an index instead: a banner per round, and an identity
# line repeated every HEARTBEAT seconds while the build runs. Because the two
# streams are captured separately, `nomad alloc logs <alloc> ci` -- stdout, the
# default -- becomes a readable timeline of rounds, and -stderr stays the
# firehose. The repetition is the point: a BEGIN banner alone is no more
# reachable than the line it replaces, whereas a heartbeat means ANY bounded
# tail answers the question.
heartbeat_pid=

start_heartbeat() {          # round check pr sha
  # Idempotent by construction. A heartbeat that outlives its round is worse
  # than none: it keeps announcing a PR this worker has stopped building, and
  # the reader has no way to tell that line from a live one. Overwriting
  # heartbeat_pid without stopping the old process would leak exactly that, so
  # starting one always ends the previous one first, whatever path got us here.
  stop_heartbeat
  [ "$HEARTBEAT" -gt 0 ] 2>/dev/null || return 0
  # set +x inside: this loop would otherwise trace itself onto stderr every
  # interval, for hours, which is the noise we are trying to escape.
  #
  # One printf per line, and a short one: writes under PIPE_BUF are atomic, so
  # a heartbeat landing mid-banner interleaves whole lines rather than
  # splicing two together.
  ( set +x
    started=$SECONDS
    while sleep "$HEARTBEAT"; do
      # Local time, not UTC, and in aliBuild's own format. stdout and stderr
      # are captured into SEPARATE files, so their relative order is lost -- a
      # heartbeat and the compiler output it describes line up only by clock.
      # aliBuild timestamps in local time, so `date -u` here produced matching
      # text two hours out and sorted the two streams into the wrong order.
      printf '%s [round %s] check=%s pr=%s sha=%.8s elapsed=%sm\n' \
             "$(date '+%Y-%m-%d@%H:%M:%S')" \
             "$1" "$2" "$3" "$4" "$(( (SECONDS - started) / 60 ))"
    done ) &
  heartbeat_pid=$!
}

stop_heartbeat() {
  [ -n "$heartbeat_pid" ] || return 0
  # Killing the subshell is enough, even though it is almost always blocked in
  # sleep(1) and that sleep is orphaned rather than killed. The orphan cannot
  # produce a stale heartbeat: the printf lives in the subshell that just died,
  # so the sleep exits silently within one interval and reaps itself.
  #
  # NOT `kill -- -$pid`: without job control this script never enables (set -m),
  # a background subshell is not a process-group leader, so $! is a PID and no
  # process group by that id exists.
  kill "$heartbeat_pid" 2>/dev/null
  # Reap it, so a worker that runs for weeks does not accumulate one zombie per
  # round.
  wait "$heartbeat_pid" 2>/dev/null
  heartbeat_pid=
}

# A worker killed mid-build (Nomad stopping the task, or a restart) would
# otherwise leave the background loop behind.
trap stop_heartbeat EXIT INT TERM

# A broken host toolchain must take THIS WORKER out, not the PRs it claims.
#
# macOS only: Linux checks build inside a container, so the host compiler says
# nothing about what they will use. On the Macs the host compiler IS the build
# environment, and an OS upgrade can leave it without a matching SDK -- Homebrew
# libc++ pulls the C library declarations in with #include_next, so the symptom
# is FP_NAN and lldiv_t being undeclared, not a legible "no SDK here".
#
# Measured cost of not having this, 2026-09-03: alibuildmac00 came back from a
# macOS 26 upgrade with no MacOSX26.sdk under CommandLineTools, was made
# eligible, and reddened about 25 O2Physics PRs in 16 minutes. Every build died
# in ~40 seconds having produced nothing, so the uploaded log read only "No
# logs found" and the GitHub status carried no reason at all. Attributing it
# afterwards took GitHub, Nomad and the S3 log bucket, because the allocation
# that did it had already been garbage collected.
#
# So: check before claiming, and skip the round rather than exiting. Exiting
# crash-loops the allocation against a machine problem no restart fixes; going
# idle leaves the worker visible, harmless, and self-healing the moment somebody
# installs the SDK. The other workers pick up the queue in the meantime.
toolchain_broken () {
  [ "$(uname)" = Darwin ] || return 1
  local _out _src _bin
  # Trailing Xs only: BSD mktemp rejects a template with anything after them,
  # so the language comes from -x c++ below rather than from a .cc suffix.
  _src=$(mktemp "${TMPDIR:-/tmp}/toolchain.XXXXXX") || return 1
  _bin="$_src.bin"
  # The two headers that actually broke, and a use of each that forces the
  # missing declarations: fpclassify needs FP_NAN, ldiv needs ldiv_t.
  printf '#include <cmath>\n#include <cstdlib>\nint main(){return std::fpclassify(1.0)+(int)ldiv(4,2).quot;}\n' > "$_src"
  if _out=$(c++ -std=c++20 -x c++ -o "$_bin" "$_src" 2>&1); then
    rm -f "$_src" "$_bin"
    return 1
  fi
  rm -f "$_src" "$_bin"
  printf '%s ===== TOOLCHAIN BROKEN on %s -- not claiming work this round =====\n%s\n' \
         "$(date '+%Y-%m-%d@%H:%M:%S')" "$(hostname)" "$_out" >&2
  return 0
}

round=0

while true; do
  round=$((round + 1))
  lister_tempfail=
  # Credentials that expire, refreshed before anything uses them.
  if [ -n "$ROUND_SETUP" ] && [ -r "$ROUND_SETUP" ]; then
    # shellcheck source=/dev/null
    . "$ROUND_SETUP"
  fi

  # The *.env files, from ali-bot@master exactly as the builders use.
  reset_git_repository ali-bot https://github.com/alisw/ali-bot || :

  # ...unless this worker is testing a candidate ali-bot, in which case the
  # config comes from the SAME ref as the code below. A PR is one thing: if it
  # changes a *.env and the script that reads it, testing them apart tests a
  # combination that will never be deployed.
  #
  # This is also what makes the override possible at all. INSTALL_ALIBOT is
  # *defined in* repo-config/DEFAULTS.env, so config fetched from master would
  # reset the pin on every round and the worker would quietly fall back to
  # master. The checkout has to move first.
  if [ -n "$ALIBOT_OVERRIDE" ]; then
    (
      cd ali-bot || exit 1
      # Detached, so reset_git_repository above leaves it alone from now on
      # (it only resets when HEAD is on a branch) and this block owns the
      # checkout. Re-fetched every round, so pushes to the PR are picked up.
      short_timeout git fetch -f "https://github.com/${ALIBOT_OVERRIDE%@*}" \
                    "+${ALIBOT_OVERRIDE#*@}:refs/ab" &&
        git checkout -f refs/ab && git clean -fxd
    ) || :
  fi

  # --all-groups because a worker must be able to walk past PRs other workers
  # have already claimed; the default output stops at the first group and would
  # leave this worker idle whenever its head entry was taken. --no-status keeps
  # the listing read-only: trust_pr would otherwise write a GitHub status from
  # here, and reporting belongs to the build, not to the survey.
  #
  # One listing per container, concatenated. The lister globs
  # repo-config/$ROLE/$CONTAINER/*.env, so it can only ever see one container at
  # a time; serving several means asking it once per container and stamping each
  # row with where it came from. The stamp goes in the *.env field, so the
  # ordering below -- which treats that field as an opaque key -- needs no
  # changes and cannot confuse two same-named checks for one.
  # Before listing, not after: a worker that cannot compile must not even look
  # at the queue, or it claims rows only to fail them.
  if toolchain_broken; then
    sleep "$IDLE_SLEEP"
    continue
  fi

  hashes=
  for _entry in $CUR_CONTAINERS; do
    _role=$(entry_role "$_entry") _container=$(entry_container "$_entry")
    # -r explicitly: the lister falls back to $MESOS_ROLE from the environment,
    # which is the job-wide role and wrong for a prefixed entry.
    # shellcheck disable=SC2086  # LISTER is a command plus flags, it must word-split
    # THE EXIT STATUS MATTERS. `|| _rows=` alone threw it away, so a rate-limited
    # lister looked exactly like a quiet queue and the loop retried on its normal
    # interval. 75 means "GitHub said wait" and drives the backoff below.
    # THE EXIT STATUS MATTERS. `|| _rows=` alone threw it away, so a rate-limited
    # lister looked exactly like a quiet queue and the loop retried on its normal
    # interval. 75 means "GitHub said wait" and drives the backoff below.
    #
    # Captured on its own line, NOT with `if ! _rows=$(...)`: inside an `if !`
    # block $? is the status of the negated test, which is always 0, so the
    # comparison below would never match and the backoff would be dead code.
    # Verified: `if ! out=$(f); then echo $?; fi` prints 0 for a function
    # returning 75.
    _rows=$(short_timeout $LISTER -r "$_role" -c "$_container")
    _lister_st=$?
    if [ "$_lister_st" != 0 ]; then
      [ "$_lister_st" = "$LISTER_TEMPFAIL" ] && lister_tempfail=1
      _rows=
    fi
    [ -n "$_rows" ] || continue
    # Key on the ENTRY, not the container, so the role survives into the claim
    # loop below -- "mesosdaq:slc9/qualitycontrol" rather than "slc9/...".
    _rows=$(printf '%s\n' "$_rows" | awk -v c="$_entry" '{ $4 = c "/" $4; print }')
    hashes="${hashes:+$hashes$'\n'}$_rows"
  done
  unset _entry _role _container

  # One lookup per *.env in this round's listing, reused for ordering, for the
  # ONLY_PRS filter and for the claim key below. Lines are
  # "env<TAB>check<TAB>only_prs<TAB>requires_pool".
  envinfo=
  if [ -n "$hashes" ]; then
    for _env in $(printf '%s\n' "$hashes" | awk '{print $4}' | sort -u); do
      envinfo="${envinfo:+$envinfo$'\n'}$_env|$(check_env_for "$_env")"
    done
  fi

  # Order the queue. The key is (group, specialisation, affinity, original
  # position):
  #
  #   group          the lister already puts untested PRs before rebuild
  #                  candidates, and that always wins -- a PR awaiting its first
  #                  verdict is what the queue exists for. Ranked by where the
  #                  lister first mentioned it, never by name, so a group this
  #                  code has not heard of cannot be silently reordered.
  #   specialisation a check pinned to THIS worker's node pool comes first,
  #                  because no other worker can take it. A GPU worker that
  #                  spends its time on work anybody could do leaves the GPU-only
  #                  queue to starve; workers with no pool of their own score
  #                  every row the same and are unaffected.
  #   affinity       then prefer the *.env built last. Everything expensive in a
  #                  work area is per-check -- sw/, the checkouts, the unpacked
  #                  tarballs -- so two builds of one check cost far less than
  #                  alternating, which evicts the other's tree each time.
  #   position       a stable final key, so staleness ordering survives inside
  #                  each bucket.
  #
  # With no pool and no previous build every row scores identically, so the order
  # is exactly what the lister produced.
  # Space-separated "env=pool" pairs, and only for checks that demand a pool --
  # usually none. NOT newline-separated: BSD awk rejects a newline inside -v, and
  # these scripts run on the macOS builders too.
  poolmap=$(printf '%s\n' "$envinfo" |
              awk -F'|' 'NF >= 4 && $4 != "" { printf "%s=%s ", $1, $4 }')

  # Per-check priority FOR THIS WORKER'S POOL, from each *.env's POOL_PRIORITY --
  # a semicolon-separated list of "pool=priority" pairs, e.g.
  #
  #     POOL_PRIORITY="m4-cluster=10;mesosdaq=5"
  #
  # Higher wins; a pool the check does not mention scores 0, so an unset
  # POOL_PRIORITY (every check today) leaves the order exactly as it was. Resolved
  # HERE rather than in awk because only this worker's own pool matters, which
  # collapses the whole table to one number per check.
  #
  # Unlike REQUIRES_POOL this is only a preference: it reorders the queue, never
  # filters it, so a worker still takes lower-priority work when nothing better is
  # waiting. That is the difference between "I am the only one who can do this"
  # and "I am the best placed to do this".
  priomap=$(printf '%s\n' "$envinfo" |
              awk -F'|' -v pool="$WORKER_NODE_POOL" '
                NF >= 5 && $5 != "" && pool != "" {
                  n = split($5, pairs, ";")
                  for (i = 1; i <= n; i++)
                    if (split(pairs[i], kv, "=") == 2 && kv[1] == pool && kv[2] + 0 != 0)
                      printf "%s=%s ", $1, kv[2] + 0
                }')

  if [ -n "$hashes" ]; then
    hashes=$(printf '%s\n' "$hashes" | awk -v pref="$last_env" -v pool="$WORKER_NODE_POOL" \
                 -v poolmap="$poolmap" -v priomap="$priomap" '
      BEGIN { n = split(poolmap, pairs, " ")
              for (i = 1; i <= n; i++)
                if (split(pairs[i], kv, "=") == 2) needs[kv[1]] = kv[2]
              n = split(priomap, pairs, " ")
              for (i = 1; i <= n; i++)
                if (split(pairs[i], kv, "=") == 2) prio[kv[1]] = kv[2] + 0 }
      { if (!($1 in grank)) grank[$1] = ++ngroups
        mine = (pool != "" && needs[$4] == pool) ? 0 : 1
        # Negated so a plain ascending sort puts the HIGHEST priority first, which
        # keeps every key in this pipeline the same direction.
        #
        # Priority sorts ABOVE the staleness group rather than within it. Within
        # it the knob was very nearly inert: the groups the lister emits are what
        # decide what a worker reaches first, so a prioritised check still waited
        # behind every ordinary row that happened to land in an earlier group.
        # That is the exact case this exists for -- the GPU builder spent its
        # rounds on alidist-dataflow, work any worker could take, while the
        # GPU-only check sat in a later group on the one machine able to run it.
        #
        # Staleness still orders everything no check prioritises, and this is a
        # no-op until something opts in: with no POOL_PRIORITY set anywhere every
        # -prio is 0 and grank decides exactly as it did before.
        #
        # Except for untested, which outranks priority. Those are PRs nobody has
        # ever built, and no amount of pool preference should stop one being
        # built: a prioritised check that is merely RED would otherwise be
        # rebuilt ahead of work that has never run at all, and a permanently red
        # PR would hold that worker indefinitely. Priority decides which REBUILD
        # is worth doing first, which is the question it was asked.
        untested = ($1 == "untested") ? 0 : 1
        # $5 (WAITING_SINCE, a unix timestamp) ABOVE the cache-affinity
        # preference, so staleness decides across containers.
        #
        # Without it the merge was arrival-ordered: this loop concatenates one
        # `list-branch-pr -c <container>` per entry in CUR_CONTAINERS, and NR --
        # the line number in that concatenation -- was the only thing separating
        # rows of the same group. So every failed row of the FIRST container
        # sorted ahead of every failed row of the LAST one, forever. On
        # 2026-09-06 that starved build/O2/fullCI_slc9: slc9-gpu is last in
        # "slc10 ubuntu2204 slc7 slc9-gpu", ~25 alidist rows sat ahead of it, and
        # five workers went seven hours without once picking it up. The lister
        # sorts each container by staleness; the merge threw that away.
        #
        # Affinity ($4 == pref, the env built last, for warm caches) still
        # breaks ties, but it can no longer park a whole container: it applies
        # WITHIN a staleness position rather than ahead of it.
        printf "%d\t%d\t%d\t%d\t%d\t%d\t%d\t%s\n", untested, -prio[$4], grank[$1], mine, $5 + 0, ($4 == pref ? 0 : 1), NR, $0 }' |
      sort -k1,1n -k2,2n -k3,3n -k4,4n -k5,5n -k6,6n -k7,7n | cut -f8-)
  fi

  built='' cooled_skipped=''
  if [ -n "$hashes" ]; then
    # A marker, because "we did not get the claim" and "the build failed" are
    # indistinguishable from an exit status: nomad var lock returns the child's
    # status when it runs one, and its own when it does not.
    marker=$(mktemp -u "${TMPDIR:-/tmp}/claim-built.XXXXXX")
    while read -r build_type pr_number pr_hash env_key waiting_since; do
      [ -n "$env_key" ] || continue
      IFS='|' read -r _ check only_prs requires_pool _pool_priority < <(
        printf '%s\n' "$envinfo" | grep -m1 -F "$env_key|")
      [ -n "$check" ] || continue

      # Back to its two halves for the build. Exported, not passed: build-one.sh
      # sources the *.env files itself, and source_env_files reads the container
      # out of the environment.
      env_name=${env_key#*/}
      # MESOS_ROLE too: source_env_files reads BOTH out of the environment, and
      # for a "role:container" entry the job-wide role is the wrong tree.
      # Assigned before export: `export x=$(...)` masks the substitution's exit
      # status (SC2155).
      # shellcheck disable=SC2031  # set here for the build, not read back from the subshell above
      CUR_CONTAINER=$(entry_container "${env_key%%/*}")
      MESOS_ROLE=$(entry_role "${env_key%%/*}")
      export CUR_CONTAINER MESOS_ROLE

      # A check can demand a particular Nomad node pool, for work that is only
      # possible on certain hardware -- REQUIRES_POOL=gpu in its *.env. This is a
      # HARD filter, not the soft preference the sort applies: a worker outside
      # that pool must never claim the row, or it takes work it cannot do and
      # fails it.
      #
      # node_pool is a job-level field in Nomad, so "both pools" means two jobs
      # sharing one queue. Partitioning the checks by directory instead would
      # work, but then a GPU worker could not help with ordinary builds when the
      # GPU queue is empty -- and being able to is the whole point of claims.
      #
      # Unset (every check today except o2-gpu-test) means any worker may take
      # it, so a worker with no WORKER_NODE_POOL behaves exactly as before.
      if [ -n "$requires_pool" ] && [ "$requires_pool" != "$WORKER_NODE_POOL" ]; then
        continue
      fi

      # ONLY_PRS: a bring-up allowlist, set per check in its *.env, so the two
      # checks a worker serves can be restricted independently -- which is the
      # point, since bringing up a platform means running a handful of PRs on it
      # while everything else stays untouched.
      #
      # Empty (the normal case, and every production check) means no filtering
      # at all. Whitespace or commas separate entries, so "6294,6300" and
      # "6294 6300" both work.
      #
      # Deliberately here and not in list-branch-pr: the lister is shared with
      # the sharded builders, and a filter there would be one edit away from
      # silently narrowing what production considers. A worker skipping rows can
      # only ever make THIS worker do less.
      if [ -n "$only_prs" ]; then
        case " ${only_prs//,/ } " in
          *" $pr_number "*) : ;;
          *) continue ;;
        esac
      fi

      # Already built here, whether or not GitHub records it. A plain string
      # rather than an associative array: macOS builders still run bash 3.2.
      #
      # Rebuilt on every lookup so expired entries fall out: this list is only
      # ever appended to, and an allocation lives for weeks.
      _now=$(date +%s)
      _kept=
      _skip=
      while IFS='|' read -r _a_check _a_sha _a_when; do
        [ -n "$_a_check" ] || continue
        # An entry with no timestamp predates the cooldown; treat it as expired
        # rather than eternal, so a rolling upgrade cannot strand a worker.
        case $_a_when in ''|*[!0-9]*) continue ;; esac
        [ $((_now - _a_when)) -lt "$CLAIM_RETRY_COOLDOWN" ] || continue
        _kept="${_kept:+$_kept$'\n'}$_a_check|$_a_sha|$_a_when"
        # An if, not `[ ... ] && _skip=1`: as the last command in the body that
        # idiom returns non-zero whenever it does not match, which would end the
        # loop the day somebody adds set -e to this script.
        if [ "$_a_check|$_a_sha" = "$check|$pr_hash" ]; then
          _skip=1
        fi
      done <<EOF
$attempted
EOF
      attempted=$_kept
      unset _a_check _a_sha _a_when _kept _now
      if [ -n "$_skip" ]; then
        unset _skip
        continue
      fi
      unset _skip

      # This check has failed fast $FAST_FAIL_LIMIT times running on THIS
      # worker; leave it alone for a while and take something else. Expiry is
      # re-evaluated here rather than pruned, so a cooldown lapses on its own.
      _now=$(date +%s)
      while IFS='|' read -r _f_check _f_n _f_when; do
        [ "$_f_check" = "$check" ] || continue
        case $_f_when in ''|*[!0-9]*) continue ;; esac
        [ "$_f_n" -ge "$FAST_FAIL_LIMIT" ] 2>/dev/null || continue
        [ $((_now - _f_when)) -lt "$FAST_FAIL_COOLDOWN" ] && _skip=1
      done <<EOF
$fastfail
EOF
      unset _f_check _f_n _f_when _now
      # Remembered for the end of the pass: if nothing gets built, this is what
      # says the cooldowns are why.
      [ -n "$_skip" ] && cooled_skipped=1
      if [ -n "$_skip" ]; then
        unset _skip
        continue
      fi

      rm -f "$marker"
      # The banner goes out BEFORE the claim is attempted, because at this point
      # we do not yet know whether we will get it -- with_claim returns having
      # done nothing if another worker holds it. The END line below says which
      # happened, so a lost claim is one cheap pair of lines, not a mystery.
      printf '%s ===== ROUND %s BEGIN check=%s pr=%s sha=%.8s type=%s =====\n' \
             "$(date '+%Y-%m-%d@%H:%M:%S')" \
             "$round" "$check" "$pr_number" "$pr_hash" "$build_type"
      claim_started=$SECONDS
      start_heartbeat "$round" "$check" "$pr_number" "$pr_hash"
      BUILD_MARKER=$marker with_claim "$check" "$pr_hash" \
        build-one.sh "$env_name" "$build_type" "$pr_number" "$pr_hash" "$waiting_since"
      build_rc=$?
      stop_heartbeat
      # Marker, not exit status: with_claim returns the child's status when it
      # ran one and its own when it did not, so only the marker distinguishes
      # "built and failed" from "never got the claim".
      if [ -e "$marker" ]; then
        # Seconds as well as minutes: a round that dies in 1s printed
        # "duration=0m", which is exactly the case worth spotting.
        printf '%s ===== ROUND %s END pr=%s result=built rc=%s duration=%sm (%ss) =====\n' \
               "$(date '+%Y-%m-%d@%H:%M:%S')" \
               "$round" "$pr_number" "$build_rc" \
               "$(( (SECONDS - claim_started) / 60 ))" \
               "$(( SECONDS - claim_started ))"
      else
        printf '%s ===== ROUND %s END pr=%s result=claim-lost =====\n' \
               "$(date '+%Y-%m-%d@%H:%M:%S')" "$round" "$pr_number"
      fi

      # ------------------------------------------------- per-check fast-fail
      #
      # A CI worker that fails builds is a worker doing its job: when a PR or a
      # dependency is broken, red is the correct answer. So consecutive FAILURES
      # are the wrong signal -- on 2026-09-19 a VecCore breakage had alimetal02
      # at 10/11 failed rounds and alimetal06 at 9/10 while both were healthy.
      #
      # What does indicate trouble is a failure too fast to have compiled
      # anything: that day separated cleanly, with infrastructure-shaped rounds
      # at 0-7s and the quickest genuine build failure at 30s.
      #
      # COOL DOWN THE CHECK, NOT THE WORKER. Sleeping the worker is wrong twice
      # over. On one sick node the damage is local -- alimetal02's fullCI area
      # was broken while its O2Physics builds succeeded all day, so stopping the
      # worker would have thrown away good capacity. And when the cause is
      # global, as an unbuildable dependency is, EVERY worker would sleep at
      # once and the queue would starve exactly when throughput is needed to
      # verify the fix. Skipping the offending check leaves every worker free to
      # build everything else, and moves capacity off the doomed check instead
      # of off the fleet.
      #
      # Gate on $marker, never on $build_rc alone: with_claim returns non-zero
      # when ANOTHER worker holds the claim, and counting those would penalise
      # precisely the healthy workers that keep losing races.
      if [ -e "$marker" ]; then
        _now=$(date +%s); _kept=; _n=0
        while IFS='|' read -r _f_check _f_n _f_when; do
          [ -n "$_f_check" ] || continue
          case $_f_when in ''|*[!0-9]*) continue ;; esac
          [ $((_now - _f_when)) -lt "$FAST_FAIL_COOLDOWN" ] || continue
          if [ "$_f_check" = "$check" ]; then _n=$_f_n; else
            _kept="${_kept:+$_kept$'\n'}$_f_check|$_f_n|$_f_when"; fi
        done <<EOF
$fastfail
EOF
        if [ "$build_rc" -ne 0 ] && [ $((SECONDS - claim_started)) -lt "$FAST_FAIL_SECONDS" ]; then
          _n=$((_n + 1))
          _kept="${_kept:+$_kept$'\n'}$check|$_n|$_now"
          if [ "$_n" -ge "$FAST_FAIL_LIMIT" ]; then
            printf '%s ===== COOLDOWN %s for %ss after %s fast failures (<%ss each) =====\n' \
                   "$(date '+%Y-%m-%d@%H:%M:%S')" "$check" "$FAST_FAIL_COOLDOWN" \
                   "$_n" "$FAST_FAIL_SECONDS" >&2
          fi
        fi
        # A success, or a failure that actually built for a while, clears this
        # check: the entry was dropped above and is simply not re-added.
        fastfail=$_kept
        unset _f_check _f_n _f_when _kept _now _n
      fi

      # Pushed EVERY round, including the healthy zero case: a series that only
      # appears when something is wrong has no baseline, and in Mimir an absent
      # series is far more awkward to alert on than one reading zero. Gauges,
      # not counters -- a dashboard must not rate() them.
      if [ -n "${OTLP_METRICS_URL:-}" ]; then
        otlp-push.py cibackoff "node=$(hostname -s)" \
                     "jobname=${NOMAD_JOB_NAME:-unknown}" \
                     -- "checks_cooling=$(printf '%s' "$fastfail" | grep -c . )" ||
          echo "claim-builder: OTLP push failed, continuing" >&2
      fi

      if [ -e "$marker" ]; then
        # We held the claim and the build ran. Re-list rather than walking on:
        # hours have passed and the queue we are holding is now a fossil.
        rm -f "$marker"
        # Recorded only when we actually built it. Losing the claim must NOT
        # count: another worker is building it, and if that worker dies this one
        # should still be able to pick it up on a later round.
        attempted="${attempted:+$attempted$'\n'}$check|$pr_hash|$(date +%s)"
        # What the work area is now warm for, used as the affinity tie-break on
        # the next round. Set from the build that RAN, not from the claim we
        # tried, so a claim lost to another worker cannot drag this one towards
        # a check it never actually built. The qualified key, because the warm
        # tree belongs to one container: slc10/o2-alidist being warm says
        # nothing about ubuntu2204/o2-alidist.
        last_env=$env_key
        built=1
        break
      fi
      # Otherwise somebody else holds it -- try the next entry immediately.
    done <<< "$hashes"
    rm -f "$marker"
  fi

  # Reclaim disk before the next round, not after the build that needs it.
  #
  # continuous-builder.sh has always done this; the claim loop was written fresh
  # and the step was never carried across. The cost of the omission, measured on
  # alimetal01 on 2026-08-28: two allocations holding 746 and 902 GiB, a root
  # filesystem at 95%, and rounds failing not on anything they built but on
  # `pip install ali-bot` -- "fatal: Unable to create temporary file ...: No
  # space left on device". `aliBuild clean` runs every round and does not touch
  # this: it only prunes BUILD dirs already unreferenced, whereas what grows is
  # the *referenced* ones -- every past build keeps its <pkg>-latest symlink, so
  # nothing it produced is ever collectable. Deleting those symlinks by age is
  # what makes the trees behind them collectable, and it is the whole win: on
  # alimetal01, ageing out 94 stale o2-alidist-dataflow builds took the
  # filesystem from 85 GiB free to 704 GiB, 95% to 61%.
  #
  # -r "$_container": cleanup.py expects build directories named after *.env
  # files directly under its ci-root, and defaults that to ".". True for the
  # sharded builders, which have <check>/sw -- but build-one.sh nests one level
  # deeper here, mkdir -p "$CUR_CONTAINER/$env_name", so ci-root has to be the
  # container directory or it silently finds nothing to clean.
  #
  # Once per container because the argument is singular. The -f pass measures
  # real free space, so later iterations no-op once the threshold is met; the -t
  # pass still ages out each container's own old builds.
  #
  # -f is an emergency floor, deliberately set below normal operating range --
  # not a target to aim for. cleanup_to_disk_threshold() stops on `not symlinks
  # or disk_free >= target`, so a target it cannot reach is not a partial
  # cleanup: it deletes EVERY symlink it gathered, across every container, and
  # only then gives up. On a node sitting under the threshold that repeats each
  # round, so every build is permanently cold -- hours per O2 build, forever.
  # 100 would have done exactly that on alimetal01, which ran at 85 GiB free.
  # Routine reclamation is -t's job; -f exists for the case where age alone did
  # not free enough and a cold rebuild beats a full disk.
  #
  # Between rounds and never during one: the loop builds a single check at a
  # time, so every other work area is idle at this point.
  for _entry in $CUR_CONTAINERS; do
    _role=$(entry_role "$_entry") _container=$(entry_container "$_entry")
    [ -d "$_container" ] || continue
    # The positional argument names the *definitions* directory,
    # <repo-config>/$MESOS_ROLE/<name>/*.env, and those live under the SUFFIXED
    # name -- so it must carry $ALIBOT_CONFIG_SUFFIX. -r stays unsuffixed
    # because build-one.sh puts the work areas in "$CUR_CONTAINER/$env_name".
    #
    # Without the suffix the release builder enumerated mesosci/slc10 (the PR
    # checks: o2-alidist, o2-gpu-test, ...) while its work area holds
    # mesosci/slc10-release ones (o2pdpsuite-daily, zlib-test). Nothing matched,
    # so it reported "found 0 symlinks in 0 of 3 environments" and deleted
    # nothing, every round, for as long as the job has existed. That is how
    # alimetal05 reached 1.3 TB in one work area and started failing other
    # people's builds with "ld: final link failed: No space left on device".
    cleanup.py -t "${CLEANUP_MAX_AGE_DAYS:-2}" -f "${CLEANUP_MIN_FREE_GIB:-50}" \
               -r "$_container" "$_role" "$_container$ALIBOT_CONFIG_SUFFIX" || true
  done
  unset _entry _role _container

  # A pass that built nothing AND skipped something only because it was cooling
  # is the one case the cooldown must never cause: the worker would sit idle on
  # its own doing. Drop every cooldown so the next pass considers everything. If
  # those checks are still broken they re-accumulate, which costs one wasted
  # round per check and cannot loop.
  if [ -z "$built" ] && [ -n "$cooled_skipped" ]; then
    printf '%s ===== COOLDOWNS CLEARED: nothing else left to build =====\n' \
           "$(date '+%Y-%m-%d@%H:%M:%S')" >&2
    fastfail=
  fi

  # Only idle when there was genuinely nothing to take. After a build, loop
  # straight back: there may be more work, and the caches are warm right now.
  # WAIT LONGER EACH TIME GITHUB SAYS WAIT. Independent of whether anything was
  # built: a worker that cannot list cannot build either, and treating that as an
  # ordinary idle round is what kept the budget empty for seven hours.
  #
  # Jittered, because the failure is fleet-wide by construction -- seventeen
  # workers hit the limit within seconds of each other, so an unjittered backoff
  # has them all return together and re-exhaust it in lockstep. A quarter of the
  # interval is enough to smear them out; $RANDOM's 15 bits of seeded PRNG are
  # plenty for spreading retries, whatever else they are not good for.
  if [ -n "$lister_tempfail" ]; then
    _wait=$(( lister_backoff + RANDOM % (lister_backoff / 4 + 1) ))
    # Clamp the WAIT, not just the backoff. Capping lister_backoff alone let the
    # jitter carry the real sleep past the cap -- measured 4324s against a 3600s
    # MAX_IDLE_SLEEP, because jitter is added on top of an already-capped value.
    [ "$_wait" -le "$MAX_IDLE_SLEEP" ] || _wait=$MAX_IDLE_SLEEP
    echo "claim: github budget exhausted; waiting ${_wait}s" >&2
    sleep "$_wait"
    lister_backoff=$(( lister_backoff * 2 ))
    [ "$lister_backoff" -le "$MAX_IDLE_SLEEP" ] || lister_backoff=$MAX_IDLE_SLEEP
    unset _wait
  else
    # A listing that worked -- even one that found nothing -- means the budget is
    # healthy, so forget any previous backoff rather than carrying it forward.
    lister_backoff=$IDLE_SLEEP
    [ -n "$built" ] || sleep "$IDLE_SLEEP"
  fi
done
