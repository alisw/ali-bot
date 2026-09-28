"""Pin that the claim loop makes progress when GitHub records nothing.

claim-builder.sh walks an ordered list and builds the first PR it can claim.
Its only *external* notion of "this one is done" is the status the build posts,
which the lister then sees and stops offering. Whenever that status is not
written -- SILENT mode during a bring-up, or a failing report-pr-errors -- the
just-built PR is still untested, still sorts first, and is picked again.

The result is a livelock rather than a slowdown: one PR is rebuilt forever and
the rest of the queue starves. Observed on slc10 before the fix: eleven builds
of PR 4966 in five minutes, on a sixteen-core node.

The sharded loop never needed protecting from this, because random.sample()
picked a different PR each round, so a missing status cost one wasted rebuild
instead of every one of them. Walking an *ordered* list is what turns it fatal,
which is why the test arrived with the claim loop and not before.

The lister stub here deliberately returns the same three PRs every time: that is
exactly what GitHub looks like when the status never gets written.
"""

import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAIM_BUILDER = os.path.join(REPO, "ci", "claim-builder.sh")

PRS = [("4966", "sha-aaa"), ("5182", "sha-bbb"), ("5838", "sha-ccc")]

HELPERS = """\
short_timeout () { "$@"; }
reset_git_repository () { :; }
source_env_files () { export CHECK_NAME="build/O2/alidist-slc10-x86"; }
"""

# Always wins the claim and always "builds", so the only thing under test is
# which PR the loop chooses next.
CLAIMS = """\
with_claim () {
  local check=$1 sha=$2; shift 2
  echo "$sha" >> "$LOGFILE"
  [ -n "$BUILD_MARKER" ] && : > "$BUILD_MARKER"
  return 0
}
"""

LISTER = "#!/bin/bash\n" + "".join(
    "printf 'untested\\t%s\\t%s\\to2-alidist\\t168243708%d\\n'\n" % (num, sha, i)
    for i, (num, sha) in enumerate(PRS))


# Two checks interleaved, plus a rebuild candidate for each. Everything
# expensive in a work area (sw/, the checkouts, unpacked tarballs) is per-check,
# so alternating between checks evicts the other's tree every time.
AFFINITY_ROWS = [
    ("untested", "1", "sha-a1", "env-one"),
    ("untested", "2", "sha-b1", "env-two"),
    ("untested", "3", "sha-a2", "env-one"),
    ("failed",   "4", "sha-b2", "env-two"),
]
# One WAITING_SINCE for every row, deliberately. Affinity is the SIXTH sort key
# in claim-builder.sh and $5 (WAITING_SINCE) is the fifth, so distinct stamps
# decide the order outright and affinity is never consulted -- the tie-break
# cannot be observed unless there is a tie. Staleness outranking affinity is the
# 2026-09-06 fullCI_slc9 fix; this fixture tests what is left to affinity.
AFFINITY_LISTER = "#!/bin/bash\n" + "".join(
    "printf '%s\\t%s\\t%s\\t%s\\t1682437000\\n'\n" % (grp, num, sha, env)
    for grp, num, sha, env in AFFINITY_ROWS)

# CHECK_NAME must vary with the *.env, or every row shares one claim key and the
# ordering cannot be observed.
AFFINITY_HELPERS = """\
short_timeout () { "$@"; }
reset_git_repository () { :; }
source_env_files () { export CHECK_NAME="build/$1"; }
"""


# One check whose builds die instantly, interleaved with one that builds fine.
# Each row is a distinct PR because a built PR enters $attempted and is not
# offered again for CLAIM_RETRY_COOLDOWN -- so repeats of the same check have to
# come from different PRs, exactly as they do in production.
FASTFAIL_ROWS = [
    ("untested", "1", "sha-bad1",  "env-bad"),
    ("untested", "2", "sha-good1", "env-good"),
    ("untested", "3", "sha-bad2",  "env-bad"),
    ("untested", "4", "sha-good2", "env-good"),
    ("untested", "5", "sha-bad3",  "env-bad"),
    ("untested", "6", "sha-good3", "env-good"),
    ("untested", "7", "sha-bad4",  "env-bad"),
    ("untested", "8", "sha-good4", "env-good"),
    ("untested", "9", "sha-bad5",  "env-bad"),
]
FASTFAIL_LISTER = "#!/bin/bash\n" + "".join(
    "printf '%s\\t%s\\t%s\\t%s\\t16824370%02d\\n'\n" % (grp, num, sha, env, i)
    for i, (grp, num, sha, env) in enumerate(FASTFAIL_ROWS))

# Every row fails fast, so cooling them all would leave the worker nothing to
# do -- the starvation the release at the end of a pass exists to prevent.
ALLBAD_ROWS = [("untested", str(i), "sha-x%d" % i, "env-bad") for i in range(1, 13)]
ALLBAD_LISTER = "#!/bin/bash\n" + "".join(
    "printf '%s\\t%s\\t%s\\t%s\\t16824370%02d\\n'\n" % (grp, num, sha, env, i)
    for i, (grp, num, sha, env) in enumerate(ALLBAD_ROWS))

# Fails instantly for env-bad and succeeds for env-good. Instantly is the point:
# the round is shorter than FAST_FAIL_SECONDS, which is what separates "the
# harness broke" from "the code is broken".
FASTFAIL_CLAIMS = """\
with_claim () {
  local check=$1 sha=$2; shift 2
  echo "$sha" >> "$LOGFILE"
  [ -n "$BUILD_MARKER" ] && : > "$BUILD_MARKER"
  case $check in *env-bad*) return 1 ;; esac
  return 0
}
"""


class ClaimBuilderAffinityTestCase(unittest.TestCase):
    """Cache affinity: prefer the check this worker just built, as a TIE-BREAK.

    The lister has no idea what any particular worker has warm, so the ordering
    has to happen in the worker. What it must not do is promote a warm rebuild
    over an untested PR of some other check: a PR waiting for its first verdict
    is the whole reason the queue exists, and warmth is only worth spending on
    ties.
    """

    @classmethod
    def setUpClass(cls):
        cls.ORDER = ClaimBuilderProgressTestCase.run_loop(
            seconds=5, lister=AFFINITY_LISTER, helpers=AFFINITY_HELPERS)

    def test_the_second_build_is_the_same_check_as_the_first(self):
        """sha-a1 (env-one) is built first, so sha-a2 (env-one) should follow --
        not sha-b1, which the lister lists in between."""
        self.assertGreaterEqual(len(self.ORDER), 3, "expected at least 3 builds: %r" % (self.ORDER,))
        self.assertEqual(self.ORDER[:3], ["sha-a1", "sha-a2", "sha-b1"],
                         "expected env-one twice before switching checks; got %r"
                         % (self.ORDER,))

    def test_untested_still_beat_a_warm_rebuild(self):
        """sha-b2 is a rebuild for env-two. Even once env-two is the warm check,
        it must not overtake sha-b1 -- and no rebuild may precede any untested
        PR."""
        untested = [row[2] for row in AFFINITY_ROWS if row[0] == "untested"]
        rebuilds = [row[2] for row in AFFINITY_ROWS if row[0] != "untested"]
        for rebuild in rebuilds:
            if rebuild not in self.ORDER:
                continue
            for u in untested:
                self.assertLess(self.ORDER.index(u), self.ORDER.index(rebuild),
                                "%s (rebuild) was built before untested %s -- affinity "
                                "must be a tie-break inside a group, not across groups"
                                % (rebuild, u))


ONLY_PRS_ROWS = [
    ("untested", "111", "sha-111", "env-one"),
    ("untested", "222", "sha-222", "env-one"),
    ("untested", "333", "sha-333", "env-one"),
    # 22 and 2222 exist to catch a substring match against the allowlisted 222.
    ("untested", "22", "sha-22", "env-one"),
    ("untested", "2222", "sha-2222", "env-one"),
]
ONLY_PRS_LISTER = "#!/bin/bash\n" + "".join(
    "printf '%s\\t%s\\t%s\\t%s\\t16824370%02d\\n'\n" % (grp, num, sha, env, i)
    for i, (grp, num, sha, env) in enumerate(ONLY_PRS_ROWS))

ONLY_PRS_HELPERS = """\
short_timeout () { "$@"; }
reset_git_repository () { :; }
source_env_files () { export CHECK_NAME="build/$1" ONLY_PRS="222,333"; }
"""


class ClaimBuilderOnlyPrsTestCase(unittest.TestCase):
    """ONLY_PRS restricts a check to a handful of PRs, for platform bring-up.

    Set per check in its *.env, so restricting one platform leaves every other
    check a worker serves untouched. It lives in the worker rather than in
    list-branch-pr on purpose: the lister is shared with the sharded production
    builders, where a filter would be one edit away from silently narrowing what
    they consider. A worker skipping rows can only make that worker do less.
    """

    @classmethod
    def setUpClass(cls):
        cls.ORDER = ClaimBuilderProgressTestCase.run_loop(
            seconds=5, lister=ONLY_PRS_LISTER, helpers=ONLY_PRS_HELPERS)

    def test_only_the_allowlisted_prs_are_built(self):
        self.assertEqual(sorted(set(self.ORDER)), ["sha-222", "sha-333"],
                         "expected only the allowlisted PRs; got %r" % (self.ORDER,))

    def test_a_pr_number_is_not_matched_as_a_substring(self):
        """22 and 2222 must not be picked up by an allowlist naming 222."""
        for sha in ("sha-22", "sha-2222"):
            self.assertNotIn(sha, self.ORDER,
                             "%s matched the allowlist as a substring" % sha)

    def test_an_empty_allowlist_does_not_filter(self):
        """Every production check leaves ONLY_PRS unset, and must be unaffected."""
        order = ClaimBuilderProgressTestCase.run_loop(seconds=5)
        self.assertEqual(sorted(set(order)), sorted(sha for _, sha in PRS))


class ClaimBuilderFastFailTestCase(unittest.TestCase):
    """A check whose builds die instantly is put on cooldown; the worker is not.

    On 2026-09-19 a VecCore breakage had alimetal02 at 10 of 11 rounds failed
    and alimetal06 at 9 of 10 while both machines were perfectly healthy: a CI
    worker failing builds is a worker doing its job. So the signal is not
    failure, it is failure too fast to have compiled anything.

    What is cooled matters as much as when. Cooling the WORKER is wrong twice
    over -- alimetal02's fullCI area was broken while its O2Physics builds
    succeeded all day, so stopping it would have thrown away good capacity; and
    when the cause is global, every worker would sleep at once and the queue
    would starve exactly when throughput is needed to verify the fix. Cooling
    the CHECK moves capacity off the broken work and leaves the rest running.
    """

    @classmethod
    def setUpClass(cls):
        cls.ORDER = ClaimBuilderProgressTestCase.run_loop(
            seconds=8, lister=FASTFAIL_LISTER, helpers=AFFINITY_HELPERS,
            claims=FASTFAIL_CLAIMS)

    def _bad(self, order=None):
        return [s for s in (order or self.ORDER) if s.startswith("sha-bad")]

    def _good(self, order=None):
        return [s for s in (order or self.ORDER) if s.startswith("sha-good")]

    def test_the_failing_check_yields_to_every_healthy_build(self):
        """Once cooled, the broken check waits until nothing else is left.

        The assertion is an ordering and not a count: the release at the end of
        a pass means a worker with no other work retries the cooled check, so
        counting attempts measures the fixture's supply of healthy rows rather
        than the cooldown.
        """
        order = self.ORDER
        last_good = max(i for i, sha in enumerate(order) if sha.startswith("sha-good"))
        self.assertIn("sha-bad4", order,
                      "fixture expects the failing check to be retried once the "
                      "healthy work runs out; got %r" % (order,))
        self.assertLess(last_good, order.index("sha-bad4"),
                        "the cooled check was retried while healthy work was "
                        "still queued; got %r" % (order,))

    def test_the_first_three_failures_are_allowed_through(self):
        """Cooling must not be hair-trigger: two fast failures are not a pattern."""
        self.assertEqual(self._bad()[:3], ["sha-bad1", "sha-bad2", "sha-bad3"],
                         "expected three attempts before cooling; got %r"
                         % (self.ORDER,))

    def test_the_worker_keeps_building_the_healthy_check(self):
        """The whole point: a cooled check must not idle the worker.

        If this fails while the others pass, the cooldown has been reattached to
        the worker instead of the check, and a fleet-wide breakage will stop
        every builder at once.
        """
        self.assertEqual(sorted(set(self._good())),
                         ["sha-good1", "sha-good2", "sha-good3", "sha-good4"],
                         "the healthy check should have drained; got %r"
                         % (self.ORDER,))

    def test_a_worker_never_idles_on_its_own_cooldowns(self):
        """Everything failing must not leave the worker with nothing to do.

        The fixed ceiling this replaced was a count of cooling checks, which was
        wrong in both directions: on a worker serving five checks it fired at
        three, disabling the cooldown while two were still buildable. The rule
        is the condition itself -- a pass that took nothing and skipped only
        cooled work drops the cooldowns -- so builds continue past the limit
        however many checks the worker serves.
        """
        order = ClaimBuilderProgressTestCase.run_loop(
            seconds=10, lister=ALLBAD_LISTER, helpers=AFFINITY_HELPERS,
            claims=FASTFAIL_CLAIMS)
        self.assertGreater(len(order), 3,
                           "the worker stopped building entirely once its only "
                           "check was cooling; got %r" % (order,))

    def test_raising_the_limit_disables_the_cooldown(self):
        """The control: without the limit the same run keeps retrying.

        Pins that it is the cooldown stopping those builds and not some other
        property of the fixture -- the failure mode that makes a green test
        meaningless.
        """
        order = ClaimBuilderProgressTestCase.run_loop(
            seconds=8, lister=FASTFAIL_LISTER, helpers=AFFINITY_HELPERS,
            claims=FASTFAIL_CLAIMS, extra_env={"FAST_FAIL_LIMIT": "999"})
        self.assertGreater(len(self._bad(order)), 3,
                           "with the limit raised the failing check should keep "
                           "being attempted; got %r" % (order,))


class ClaimBuilderProgressTestCase(unittest.TestCase):
    #: Every assertion here reads the same run, because the run costs wall-clock
    #: (the loop has to be timed out) and nothing below mutates it.
    ORDER = None

    @classmethod
    def setUpClass(cls):
        cls.ORDER = cls.run_loop()

    @staticmethod
    def run_loop(seconds=4, lister=None, helpers=None, claims=None, extra_env=None):
        """Run the real claim-builder.sh against stubs; return what it built."""
        with tempfile.TemporaryDirectory() as tree:
            binpath = os.path.join(tree, "bin")
            os.mkdir(binpath)
            for name, body in (("build-helpers.sh", helpers or HELPERS),
                               ("claims.sh", claims or CLAIMS),
                               ("list-branch-pr", lister or LISTER)):
                path = os.path.join(binpath, name)
                with open(path, "w") as handle:
                    handle.write(body)
                os.chmod(path, 0o755)

            log = os.path.join(tree, "built.log")
            open(log, "w").close()
            # CUR_CONTAINER explicitly, and not merely inherited: claim-builder
            # defaults CUR_CONTAINERS to it, and the row loop is `for _container
            # in $CUR_CONTAINERS`, so leaving it unset iterates zero times and
            # the loop builds nothing at all -- every assertion here then fails
            # as an empty list, which reads like a logic bug rather than a
            # missing variable. Setting it also stops the result depending on
            # whether the developer happens to have it exported.
            env = dict(os.environ,
                       PATH=binpath + os.pathsep + os.environ["PATH"],
                       CUR_CONTAINER="slc10",
                       LOGFILE=log, IDLE_SLEEP="1", HOME=tree,
                       **(extra_env or {}))
            # The loop never exits by design, so stop it and read what it did.
            try:
                subprocess.run(["bash", CLAIM_BUILDER], env=env, timeout=seconds,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                pass
            with open(log) as handle:
                return handle.read().split()

    def test_every_pr_is_built_even_though_no_status_is_recorded(self):
        """The queue drains although the lister's answer never changes."""
        built = self.ORDER
        self.assertEqual(sorted(set(built)), sorted(sha for _, sha in PRS),
                         "expected every PR to be built; got %r" % (built,))

    def test_no_pr_is_built_twice(self):
        """The regression. Unfixed, this logs one PR several hundred times."""
        built = self.ORDER
        repeated = {sha for sha in built if built.count(sha) > 1}
        self.assertFalse(repeated,
                         "rebuilt %s without any new commit -- the loop is not "
                         "advancing, which starves the rest of the queue"
                         % sorted(repeated))

    def test_it_stops_instead_of_spinning_once_everything_is_built(self):
        """Having exhausted the queue it must idle, not re-walk it.

        Bounded well above the three real builds but far below the hundreds a
        spinning loop reaches, so this fails on a hot loop without being timing
        sensitive.
        """
        built = self.ORDER
        self.assertLess(len(built), 10,
                        "%d builds for %d PRs means the loop is spinning"
                        % (len(built), len(PRS)))

    def test_the_git_identity_is_set(self):
        """Without it the PR merge dies with 'fatal: empty ident name' before
        any compilation, which is how the first slc10 deployment failed."""
        with open(CLAIM_BUILDER) as handle:
            body = handle.read()
        self.assertIn("git config --global user.name", body)
        self.assertIn("git config --global user.email", body)


if __name__ == "__main__":
    unittest.main()


class TestContainerEntryRole(unittest.TestCase):
  """An entry in CUR_CONTAINERS may carry the repo-config tree to read.

  repo-config is <role>/<container>/*.env, and the role is NOT derivable from
  the container: mesosci and mesosdaq both have an "slc9" directory holding
  completely different checks. Before this, the role was one job-wide variable,
  so serving the mesosdaq queue meant a second Nomad job whose only difference
  was MESOS_ROLE -- and its ten checks sat behind a single builder that could
  not borrow idle capacity from the rest of the fleet.

  The bare form must keep working unchanged: every existing job passes plain
  container names and expects $MESOS_ROLE.
  """

  def parse(self, entry, mesos_role="mesosci"):
    """Run the two helpers out of claim-builder.sh, as the loop does."""
    script = (
      'MESOS_ROLE=%s\n'
      '%s\n'
      'printf "%%s %%s" "$(entry_role %s)" "$(entry_container %s)"\n'
    ) % (mesos_role, self.helpers(), entry, entry)
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    self.assertEqual(out.returncode, 0, out.stderr)
    return tuple(out.stdout.split())

  @staticmethod
  def helpers():
    """The helper definitions, lifted verbatim so the test cannot drift."""
    with open(CLAIM_BUILDER) as fh:
      src = fh.read()
    return "\n".join(l for l in src.splitlines()
                      if l.startswith("entry_role ()") or l.startswith("entry_container ()"))

  def test_a_bare_container_uses_the_job_wide_role(self):
    self.assertEqual(self.parse("slc9"), ("mesosci", "slc9"))

  def test_a_prefixed_entry_overrides_the_role(self):
    self.assertEqual(self.parse("mesosdaq:slc9"), ("mesosdaq", "slc9"))

  def test_the_same_container_name_resolves_to_different_trees(self):
    """The whole point: mesosci/slc9 and mesosdaq/slc9 are different checks."""
    self.assertEqual(self.parse("mesosci:slc9")[0], "mesosci")
    self.assertEqual(self.parse("mesosdaq:slc9")[0], "mesosdaq")
    self.assertEqual(self.parse("mesosci:slc9")[1],
                     self.parse("mesosdaq:slc9")[1])

  def test_a_suffixed_container_still_parses(self):
    """Names with a dash are ordinary -- only ':' is special."""
    self.assertEqual(self.parse("slc9-o2physics"), ("mesosci", "slc9-o2physics"))
    self.assertEqual(self.parse("mesosdaq:slc9-gpu"), ("mesosdaq", "slc9-gpu"))

  def test_the_job_wide_role_is_honoured_for_bare_entries(self):
    self.assertEqual(self.parse("slc9", mesos_role="mesosdaq"), ("mesosdaq", "slc9"))


if __name__ == "__main__":
  unittest.main()

