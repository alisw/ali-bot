"""A comment in a *.env file must never change what the file parses to.

parse_env_file feeds shlex with comments=False, so before strip_env_comments
existed the comment text was tokenised as shell words. A single apostrophe in
prose -- "the day's tag", "this check's failures" -- opened a quotation that was
never closed, shlex raised "No closing quotation", and the generator yielded
NOTHING.

That is silent exactly where it hurts: list-branch-pr drops the check from its
listing, claim-builder.sh sees an empty row set and `continue`s past the
container, and the check stops being built with no error logged anywhere. It
removed build/O2/alidist-ubuntu2204 from the queue for an afternoon, and two
mesosci/slc10-release/*.env files were in the same state at the same time.

`bash -n` does NOT catch this -- bash is perfectly happy with an apostrophe in a
comment. Only this parser cares, so only a test against this parser helps.
"""
import os
import tempfile
import unittest

from alibot_helpers.utilities import parse_env_file, strip_env_comments


class ParseEnvFileTestCase(unittest.TestCase):
    def parse(self, body):
        """parse_env_file over a temporary file holding `body`."""
        fd, path = tempfile.mkstemp(suffix=".env")
        try:
            with os.fdopen(fd, "w") as envf:
                envf.write(body)
            return dict(parse_env_file(path))
        finally:
            os.unlink(path)

    def test_an_apostrophe_in_a_comment_is_harmless(self):
        """The regression this exists for. One apostrophe used to lose the file."""
        self.assertEqual(
            self.parse("# the day's tag, and this check's failures\nFOO=bar\n"),
            {"FOO": "bar"})

    def test_an_odd_number_of_quotes_across_several_comments(self):
        """Comments balancing each other by accident is not a defence."""
        self.assertEqual(
            self.parse("# it was 'x-*' during bring-up\n"
                       "# which forces the day's tag\n"
                       "CHECK_NAME=build/O2/alidist-ubuntu2204\n"),
            {"CHECK_NAME": "build/O2/alidist-ubuntu2204"})

    def test_a_hash_inside_a_value_survives(self):
        """Why comments are stripped by line rather than handed to shlex.

        shlex(comments=True) would truncate this value at the '#'.
        """
        self.assertEqual(self.parse("FOO=a#b\n"), {"FOO": "a#b"})

    def test_a_multi_line_quoted_value_survives(self):
        """DEVEL_PKGS is written this way in every check that pins a repo."""
        self.assertEqual(
            self.parse('DEVEL_PKGS="alisw/alidist master\n'
                       'AliceO2Group/AliceO2 dev O2"\n'),
            {"DEVEL_PKGS": "alisw/alidist master\nAliceO2Group/AliceO2 dev O2"})

    def test_an_indented_comment_is_still_a_comment(self):
        self.assertEqual(self.parse("   # what's this\nFOO=bar\n"), {"FOO": "bar"})

    def test_a_comment_cannot_start_mid_line(self):
        """Only whole-line comments are stripped, so a value keeps its '#'."""
        self.assertEqual(self.parse("FOO=bar#baz\n"), {"FOO": "bar#baz"})

    def test_a_stripped_comment_keeps_its_line(self):
        """A comment becomes an empty line, not nothing.

        So the line number of everything after it is unchanged, and any future
        error message points at the right line of the original file.
        """
        lines = strip_env_comments("# a\n# b\nFOO=1\n").splitlines()
        self.assertEqual(lines, ["", "", "FOO=1"])
        self.assertEqual(lines.index("FOO=1"), 2)  # still the third line


if __name__ == "__main__":
    unittest.main()
