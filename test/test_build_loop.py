"""Pin the field parsing in build-loop.sh's queued-status loop.

Before building, build-loop.sh walks the rest of the queue and marks each PR
with its position ("Queued (N ahead)"). It does that by piping list-branch-pr's
output through `cat -n` and reading the fields back in shell.

That pipeline is fragile in a way that fails *silently*: `cat -n` prepends a
line number, so each line carries one more field than list-branch-pr printed,
and `read` folds every surplus field into its last variable. Get the count
wrong and the environment name silently acquires the trailing column, the *.env
file is not found, CHECK_NAME and PR_REPO are never set, and the status is
posted with empty values or not at all. No error is raised anywhere.

It only ever broke for *untested* PRs, because they are the only rows carrying a
non-empty waiting_since; on rebuild rows the trailing tab is stripped as
whitespace and the same code works by accident. So the failure was confined to
exactly the case the loop exists for.

The test runs the real line out of build-loop.sh rather than a copy of it, so
it cannot drift away from the code it is protecting.
"""

import os
import re
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_LOOP = os.path.join(REPO, "ci", "build-loop.sh")

# One untested PR and one rebuild candidate, in the five tab-separated fields
# list-branch-pr emits: TYPE, NUMBER, SHA, ENV, WAITING_SINCE.
FIXTURE = (
    "untested\\t4966\\tsha-a\\to2-alidist\\t1682437081\\n"
    "untested\\t5182\\tsha-b\\to2-alidist\\t1699999999\\n"
    "failed\\t5838\\tsha-c\\to2-alidist\\t\\n"
)

# The four-field form this loop was originally written for, before
# waiting_since was appended. Builders pip-install ali-bot per iteration, so
# during a rollout they can be on either version, and the loop has to parse
# both. Three rows, so that more than one survives the `tail` and the
# line-boundary behaviour is actually exercised.
FIXTURE_LEGACY = (
    "untested\\t4966\\tsha-a\\to2-alidist\\n"
    "untested\\t5182\\tsha-b\\to2-alidist\\n"
    "untested\\t5838\\tsha-c\\to2-alidist\\n"
)


class QueuedStatusParsingTestCase(unittest.TestCase):
    def read_loop_line(self):
        """The actual `cat -n | while read ...` line from build-loop.sh."""
        with open(BUILD_LOOP) as handle:
            for line in handle:
                if "cat -n" in line and "while read" in line:
                    return line.strip()
        self.fail("could not find the queued-status loop in build-loop.sh")

    def parse_fixture(self, fixture=None):
        """Run that line over a fixture, reporting what it bound to each name."""
        loop = self.read_loop_line()
        # Replace the loop body with something that just reports the fields.
        loop = loop.split("; do")[0] + '; do printf "%s|%s|%s\\n" "$btype" "$num" "$envf"; done'
        script = ('HASHES=$(printf "%s")\nBUILD_SEQ=1\n%s\n'
                  % (fixture if fixture is not None else FIXTURE, loop))
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return [line.split("|") for line in out.stdout.splitlines()]

    def test_env_name_does_not_absorb_the_waiting_since(self):
        """The regression: envf must be the *.env basename and nothing else.

        A tab in it means the read has fewer variables than the piped line has
        fields, and source_env_files will look for a file that cannot exist.
        """
        for btype, num, envf in self.parse_fixture():
            self.assertNotIn("\t", envf,
                             "envf %r carries a trailing column for PR %s" % (envf, num))
            self.assertEqual(envf, "o2-alidist",
                             "envf %r is not the environment name for PR %s"
                             % (envf, num))

    def test_untested_rows_are_parsed_like_rebuild_rows(self):
        """Untested rows carry a waiting_since and rebuild rows do not, which is
        precisely why only the former used to break."""
        parsed = self.parse_fixture()
        self.assertTrue(parsed, "the loop produced no rows")
        self.assertEqual({envf for _, _, envf in parsed}, {"o2-alidist"})

    def test_the_queue_position_counts_from_one(self):
        """BUILD_SEQ=1 means the head of the list is being built now, so the
        rest are numbered from 1 as 'ahead' of nothing."""
        loop = self.read_loop_line().split("; do")[0]
        script = ('HASHES=$(printf "%s")\nBUILD_SEQ=1\n%s; do printf "%%s\\n" "$ahead"; done\n'
                  % (FIXTURE, loop))
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(out.stdout.split(), ["1", "2"],
                         "expected the two PRs behind the head, numbered from 1")

    def test_the_four_field_form_still_parses(self):
        """Backward compatibility, for builders mid-rollout.

        With one field fewer than variables, the surplus variable is simply
        empty -- `read` consumes exactly one line and never reaches across the
        newline for it. Adding a variable is therefore safe against the older
        output as well as the current one.
        """
        parsed = self.parse_fixture(FIXTURE_LEGACY)
        self.assertEqual({envf for _, _, envf in parsed}, {"o2-alidist"})

    def test_rows_are_parsed_independently(self):
        """The specific worry: a missing trailing field must not pull the first
        field of the following row into this one."""
        parsed = self.parse_fixture(FIXTURE_LEGACY)
        self.assertEqual([num for _, num, _ in parsed], ["5182", "5838"],
                         "each row must contribute exactly its own PR number")

    def test_the_loop_reads_raw(self):
        """-r is load-bearing, not decoration.

        Without it a field ending in a backslash splices the next line in, and
        the following row's first field really is stolen -- the one way `read`
        can cross a line boundary. These fields carry branch names, so a
        trailing backslash is not unthinkable.
        """
        self.assertIn("read -r", self.read_loop_line(),
                      "the queued-status loop must read raw")

if __name__ == "__main__":
    unittest.main()
