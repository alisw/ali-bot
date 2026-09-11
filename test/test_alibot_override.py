"""Pin ALIBOT_OVERRIDE, the knob that runs a candidate ali-bot in a checker.

ali-bot reaches a claimed worker by three separate routes: the jobspec's
bootstrap install (which provides claim-builder.sh, the loop itself), the
per-build pip install in build-one.sh, and the repo-config checkout that
supplies the *.env files. Testing a PR means moving all three, because a worker
running new code against master's configuration is a combination that will
never be deployed.

The third route is also what makes the other two possible. INSTALL_ALIBOT is
*defined in* repo-config/DEFAULTS.env, so as long as the checkout tracks master
it rewrites the pin on every round and the worker silently falls back to master.
That is why the override moves the checkout first and why it is applied after
source_env_files rather than through it.

The bootstrap install lives in the jobspec (ci-jobs/ci-slc10.nomad) and is not
covered here; these tests cover the two routes that live in this repository.
"""

import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_ONE = os.path.join(REPO, "ci", "build-one.sh")
CLAIM_BUILDER = os.path.join(REPO, "ci", "claim-builder.sh")

PINNED = "alisw/ali-bot@master"
CANDIDATE = "alisw/ali-bot@refs/pull/1725/head"

# What repo-config/DEFAULTS.env does: a plain assignment, which overwrites
# anything already in the environment. Reproduced faithfully, because it is the
# reason the override cannot simply be exported into the job.
HELPERS = """\
short_timeout () { echo "$@" >> "$LOGFILE"; return 0; }
source_env_files () {
  INSTALL_ALIBOT=%s
  INSTALL_ALIBUILD=alisw/alibuild@v1.17.43
  CHECK_NAME=build/O2/alidist-slc10-x86
}
""" % PINNED


class BuildOneOverrideTestCase(unittest.TestCase):
    def pip_spec(self, override=None):
        """Run build-one.sh and return the ali-bot it tried to install."""
        with tempfile.TemporaryDirectory() as tree:
            binpath = os.path.join(tree, "bin")
            os.mkdir(binpath)
            for name, body in (("build-helpers.sh", HELPERS),
                               ("build-loop.sh", ": # the build itself, stubbed\n")):
                with open(os.path.join(binpath, name), "w") as handle:
                    handle.write(body)

            log = os.path.join(tree, "pip.log")
            open(log, "w").close()
            # build-one.sh:113 does mkdir -p "${CUR_CONTAINER:?}/$env_name",
            # so without it the script exits before ever reaching the pip
            # install these tests are about, and every assertion fails against
            # an empty log. Set rather than inherited, so the result does not
            # depend on whether the developer has it exported.
            env = dict(os.environ,
                       PATH=binpath + os.pathsep + os.environ["PATH"],
                       CUR_CONTAINER="slc10",
                       LOGFILE=log)
            env.pop("ALIBOT_OVERRIDE", None)
            if override is not None:
                env["ALIBOT_OVERRIDE"] = override

            subprocess.run(["bash", BUILD_ONE, "o2-alidist", "untested",
                            "4966", "sha-aaa", "1682437081"],
                           cwd=tree, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with open(log) as handle:
                return handle.read()

    def test_the_env_file_pin_is_used_when_nothing_is_overridden(self):
        """Backward compatibility: unset must behave exactly as before."""
        self.assertIn(PINNED, self.pip_spec())

    def test_the_candidate_wins_over_the_env_file_pin(self):
        spec = self.pip_spec(CANDIDATE)
        self.assertIn(CANDIDATE, spec)
        self.assertNotIn(PINNED, spec,
                         "the *.env pin must not also be installed -- pip would "
                         "take whichever came last, which is not a decision to "
                         "leave to argument order")

    def test_an_empty_override_is_the_same_as_unset(self):
        """The jobspec ships ALIBOT_OVERRIDE="" as the default, so empty has to
        mean master rather than an empty git URL."""
        self.assertIn(PINNED, self.pip_spec(""))


class ClaimBuilderCheckoutTestCase(unittest.TestCase):
    def test_the_repo_config_checkout_follows_the_override(self):
        """Without this the *.env files come from master, and since they set
        INSTALL_ALIBOT they would reset the pin on every round."""
        with open(CLAIM_BUILDER) as handle:
            body = handle.read()
        self.assertIn("ALIBOT_OVERRIDE", body)
        self.assertIn("git fetch", body)
        # The ref half of "owner/repo@ref", fetched into a local ref.
        self.assertIn("${ALIBOT_OVERRIDE#*@}", body)
        # The owner/repo half, so a fork can be tested and not just alisw.
        self.assertIn("${ALIBOT_OVERRIDE%@*}", body)

    def test_the_override_block_is_skipped_when_unset(self):
        """Production and the default deployment must not fetch anything extra."""
        with open(CLAIM_BUILDER) as handle:
            body = handle.read()
        self.assertIn('if [ -n "$ALIBOT_OVERRIDE" ]; then', body)


if __name__ == "__main__":
    unittest.main()
