"""Pin when a claimed row is skipped because its listing is out of date.

The claim lock serialises builds of one (check, sha), but it is released as
soon as the holder posts its verdict. A worker whose listing predates that
verdict wins the free lock and would build the check all over again: on
alidist#6413 that was six to nine hours a time, back to back. So build-one.sh
re-reads the status once it holds the claim (set-github-status --print-state)
and asks stale_listing whether the status changed since the listing.
"""

import calendar
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from unittest import mock

from alibot_helpers.github_utilities import githubStateLine

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPERS = os.path.join(REPO, "ci", "build-helpers.sh")
BUILD_ONE = os.path.join(REPO, "ci", "build-one.sh")
SET_GITHUB_STATUS = os.path.join(REPO, "set-github-status")


def stale_listing(*args):
    """Run stale_listing from the real build-helpers.sh: (rc, stdout, stderr)."""
    p = subprocess.run(["bash", "-c", '. "$0"; stale_listing "$@"', HELPERS]
                       + list(args), capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def read_and_decide(line, build_type, since):
    """Parse a --print-state line exactly as build-one.sh does, then decide."""
    p = subprocess.run(["bash", "-c", '. "$0"; read -r state created id; '
                        'stale_listing "$1" "$2" "$state" "$created"',
                        HELPERS, build_type, since],
                       input=line + "\n", capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


class StaleListingTestCase(unittest.TestCase):
    def assertSkip(self, *args):
        rc, out, err = stale_listing(*args)
        self.assertEqual((rc, err), (0, ""), args)
        self.assertTrue(out.startswith("stale-skip: a newer %s status" % args[2]), out)

    def assertBuild(self, *args):
        """Build, quietly: no reason printed and nothing on stderr."""
        self.assertEqual(stale_listing(*args), (1, "", ""), args)

    # An untested row's WAITING_SINCE is the commit time, which says nothing
    # about the status, so only the state may decide.
    def test_an_untested_row_with_a_verdict_is_skipped(self):
        for state in ("success", "failure", "error"):
            self.assertSkip("untested", "1000", state, "2000")

    def test_an_untested_row_without_a_verdict_is_built(self):
        """pending: the previous holder died and its claim lapsed; none: new."""
        for state in ("pending", "none"):
            self.assertBuild("untested", "1000", state, "2000")

    # A rebuild row's WAITING_SINCE is the createdAt of the status it was
    # listed for, so any later status is a different one.
    def test_a_rebuild_row_with_a_newer_status_is_skipped(self):
        """The state may well be the same before and after; the time tells."""
        for build_type, state in (("failed", "error"), ("failed", "success"),
                                  ("succeeded", "success"), ("failed", "pending")):
            self.assertSkip(build_type, "1000", state, "2000")

    def test_a_rebuild_row_with_the_listed_status_is_built(self):
        for build_type, state in (("failed", "error"), ("succeeded", "success")):
            self.assertBuild(build_type, "1000", state, "1000")

    def test_anything_empty_or_unknown_builds_quietly(self):
        for build_type in ("untested", "failed", "succeeded", "bogus", ""):
            self.assertBuild(build_type, "1000", "", "")
        # Each value on its own: "" and "1000" must not pass as "1000".
        for since, created in (("1000", ""), ("", "2000"), ("1000", "-"),
                               ("-", "2000"), ("0", "2000"), ("x", "2000"),
                               ("1000", "20x0")):
            self.assertBuild("failed", since, "error", created)


class FakeGH:
    def __init__(self, statuses):
        self.statuses = statuses

    def get(self, *args, **kwds):
        return self.statuses


CREATED = "2026-11-10T00:26:22Z"
EPOCH = calendar.timegm((2026, 11, 10, 0, 26, 22))


class GithubStateLineTestCase(unittest.TestCase):
    """The --print-state line is a contract with build-one.sh's `read`."""

    def test_the_newest_status_of_the_context_wins(self):
        """GitHub lists statuses newest first."""
        gh = FakeGH([
            {"context": "build/other", "state": "failure", "id": 1},
            {"context": "build/X", "state": "success", "created_at": CREATED, "id": 42},
            {"context": "build/X", "state": "pending", "created_at": "2026-11-09T15:40:06Z", "id": 41},
        ])
        self.assertEqual(githubStateLine(gh, "alisw/alidist@abc", "build/X"),
                         "success %d 42" % EPOCH)

    def test_no_status_for_the_context(self):
        gh = FakeGH([{"context": "build/other", "state": "success"}])
        self.assertEqual(githubStateLine(gh, "alisw/alidist@abc", "build/X"), "none - -")

    def test_missing_fields_keep_their_place(self):
        """An empty field would let `read` shift the id into CREATED."""
        gh = FakeGH([{"context": "build/X", "state": "error", "id": 42}])
        self.assertEqual(githubStateLine(gh, "alisw/alidist@abc", "build/X"), "error - 42")

    def test_an_unreadable_status_list_raises(self):
        with self.assertRaises(RuntimeError):
            githubStateLine(FakeGH(None), "alisw/alidist@abc", "build/X")

    def test_build_one_parses_each_shape(self):
        self.assertEqual(read_and_decide("error %d 42" % EPOCH, "failed", str(EPOCH - 1))[0], 0)
        self.assertEqual(read_and_decide("error %d 42" % EPOCH, "failed", str(EPOCH)), (1, "", ""))
        self.assertEqual(read_and_decide("error - 42", "failed", "1000"), (1, "", ""))
        self.assertEqual(read_and_decide("none - -", "untested", "1000"), (1, "", ""))
        self.assertEqual(read_and_decide("", "failed", "1000"), (1, "", ""))


def load_set_github_status():
    loader = SourceFileLoader("set_github_status", SET_GITHUB_STATUS)
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


class PrintStateTestCase(unittest.TestCase):
    def run_main(self, argv, statuses=None):
        """set-github-status's main() against a fake GitHub: (exit, stdout, stderr)."""
        module = load_set_github_status()
        client = mock.MagicMock()
        client.return_value.__enter__.return_value = FakeGH(statuses)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(module, "GithubCachedClient", client), \
             mock.patch.object(sys, "argv", ["set-github-status"] + argv), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             self.assertRaises(SystemExit) as exit:
            module.main()
        return exit.exception.code, out.getvalue(), err.getvalue()

    def test_it_prints_one_line(self):
        code, out, _ = self.run_main(
            ["--print-state", "-c", "alisw/alidist@abc", "--context", "build/X"],
            [{"context": "build/X", "state": "success", "created_at": CREATED, "id": 42}])
        self.assertEqual((code, out), (0, "success %d 42\n" % EPOCH))

    def test_an_unreadable_status_is_one_line_and_exit_1(self):
        code, out, err = self.run_main(
            ["--print-state", "-c", "alisw/alidist@abc", "--context", "build/X"])
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(len(err.splitlines()), 1, err)

    def test_the_arguments_each_mode_needs(self):
        self.assertEqual(self.run_main(["--print-state", "-c", "alisw/alidist@abc"])[0], 2)
        self.assertEqual(self.run_main(["-c", "alisw/alidist@abc"])[0], 2)


# build-one.sh with the real stale_listing and stubs for everything slow.
BUILD_ONE_HELPERS = """\
. %s
general_timeout () { shift; echo "$@" >> "$LOGFILE"; }
short_timeout () {
  case $1 in set-github-status) "$@" ;; *) echo "$@" >> "$LOGFILE" ;; esac
}
source_env_files () {
  CHECK_NAME=build/O2/fullCI PR_REPO=alisw/alidist
  INSTALL_ALIBOT=alisw/ali-bot@master INSTALL_ALIBUILD=alisw/alibuild@v1.17.43
}
""" % HELPERS

# What GitHub says now, or an ali-bot that predates --print-state.
SET_GITHUB_STATUS_STUB = """\
#!/bin/bash
case " $* " in
  *" --help "*) [ -n "$NO_PRINT_STATE" ] && exit 2; echo "  --print-state" ;;
  *) printf '%s\\n' "$STATE_LINE" ;;
esac
"""


def run_build_one(build_type, since, state_line, **env_extra):
    """Run build-one.sh; return (rc, marker written, skip reason, log, stderr)."""
    with tempfile.TemporaryDirectory() as tree:
        binpath = os.path.join(tree, "bin")
        os.mkdir(binpath)
        for name, body in (("build-helpers.sh", BUILD_ONE_HELPERS),
                           ("build-loop.sh", 'echo build-loop >> "$LOGFILE"\n'),
                           ("set-github-status", SET_GITHUB_STATUS_STUB)):
            path = os.path.join(binpath, name)
            with open(path, "w") as handle:
                handle.write(body)
            os.chmod(path, 0o755)
        log, marker = os.path.join(tree, "log"), os.path.join(tree, "marker")
        open(log, "w").close()
        env = dict(os.environ, PATH=binpath + os.pathsep + os.environ["PATH"],
                   CUR_CONTAINER="slc9", LOGFILE=log, BUILD_MARKER=marker,
                   STATE_LINE=state_line, **env_extra)
        p = subprocess.run(["bash", BUILD_ONE, "fullci", build_type, "6413",
                            "sha-aaa", since], cwd=tree, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        skipped = None
        if os.path.exists(marker + ".skipped"):
            with open(marker + ".skipped") as handle:
                skipped = handle.read().strip()
        with open(log) as handle:
            return p.returncode, os.path.exists(marker), skipped, handle.read(), p.stderr


class BuildOneStaleListingTestCase(unittest.TestCase):
    def test_a_stale_row_is_skipped_before_anything_runs(self):
        for build_type, line in (("untested", "success 2000 42"),
                                 ("failed", "error 2000 42")):
            rc, built, skipped, log, _ = run_build_one(build_type, "1000", line)
            self.assertEqual((rc, built, skipped), (0, False, "stale-skip"), build_type)
            self.assertNotIn("build-loop", log)
            self.assertNotIn("pip install", log)

    def test_a_fresh_row_builds(self):
        rc, built, skipped, log, _ = run_build_one("untested", "1000", "pending 2000 42")
        self.assertEqual((built, skipped), (True, None))
        self.assertIn("build-loop", log)

    def test_one_line_per_decision(self):
        """With the listed and the current status, to diagnose duplicates."""
        for line, decision in (("error 2000 42", "stale-skip: a newer error status"),
                               ("error 1000 41", "build")):
            *_, err = run_build_one("failed", "1000", line)
            said = [l for l in err.splitlines() if l.startswith("build-one: ")]
            self.assertEqual(len(said), 1, err)
            self.assertIn("listed failed since=1000, now error", said[0])
            self.assertIn(": " + decision, said[0])

    def test_an_old_ali_bot_builds_and_says_so_once(self):
        rc, built, skipped, log, err = run_build_one(
            "untested", "1000", "success 2000 42", NO_PRINT_STATE="1")
        self.assertEqual((built, skipped), (True, None))
        self.assertIn("build-loop", log)
        said = [l for l in err.splitlines() if l.startswith("build-one: ")]
        self.assertEqual(len(said), 1, err)
        self.assertIn("no --print-state", said[0])


if __name__ == "__main__":
    unittest.main()
