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
AFFINITY_LISTER = "#!/bin/bash\n" + "".join(
    "printf '%s\\t%s\\t%s\\t%s\\t16824370%02d\\n'\n" % (grp, num, sha, env, i)
    for i, (grp, num, sha, env) in enumerate(AFFINITY_ROWS))

# CHECK_NAME must vary with the *.env, or every row shares one claim key and the
# ordering cannot be observed.
AFFINITY_HELPERS = """\
short_timeout () { "$@"; }
reset_git_repository () { :; }
source_env_files () { export CHECK_NAME="build/$1"; }
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


class ClaimBuilderProgressTestCase(unittest.TestCase):
    #: Every assertion here reads the same run, because the run costs wall-clock
    #: (the loop has to be timed out) and nothing below mutates it.
    ORDER = None

    @classmethod
    def setUpClass(cls):
        cls.ORDER = cls.run_loop()

    @staticmethod
    def run_loop(seconds=4, lister=None, helpers=None):
        """Run the real claim-builder.sh against stubs; return what it built."""
        with tempfile.TemporaryDirectory() as tree:
            binpath = os.path.join(tree, "bin")
            os.mkdir(binpath)
            for name, body in (("build-helpers.sh", helpers or HELPERS),
                               ("claims.sh", CLAIMS),
                               ("list-branch-pr", lister or LISTER)):
                path = os.path.join(binpath, name)
                with open(path, "w") as handle:
                    handle.write(body)
                os.chmod(path, 0o755)

            log = os.path.join(tree, "built.log")
            open(log, "w").close()
            env = dict(os.environ,
                       PATH=binpath + os.pathsep + os.environ["PATH"],
                       LOGFILE=log, IDLE_SLEEP="1", HOME=tree)
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
