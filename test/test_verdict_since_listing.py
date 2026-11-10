"""Pin when a claimed row is skipped because another worker already built it.

The claim lock serialises builds of one (check, sha), but it is released as
soon as the holder posts its verdict. A worker whose listing predates that
verdict still holds the row as untested, wins the now free lock and would build
the check again. build-one.sh therefore re-reads the status after claiming and
asks verdict_since_listing whether the row is out of date: by state for an
untested row, and by the time of the newest status for a rebuild row.
"""

import os
import subprocess
import unittest

from alibot_helpers.github_utilities import latestGithubState

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPERS = os.path.join(REPO, "ci", "build-helpers.sh")


def stale(build_type, state, created="", since=""):
  """Exit status of verdict_since_listing, run from the real build-helpers.sh."""
  return subprocess.run(
    ["bash", "-c", '. "$0" 2>/dev/null; verdict_since_listing "$@"',
     HELPERS, build_type, state, created, since],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


class VerdictSinceListingTestCase(unittest.TestCase):
  # For untested rows WAITING_SINCE is the commit time, which says nothing
  # about the status, so only the state may decide.
  def test_an_untested_row_with_a_verdict_is_skipped(self):
    for state in ("success", "failure", "error"):
      self.assertTrue(stale("untested", state, "2000", "1000"), state)

  def test_an_untested_row_without_a_verdict_is_built(self):
    """pending: the previous holder died and its claim lapsed; none: new."""
    for state in ("pending", "none"):
      self.assertFalse(stale("untested", state, "2000", "1000"), state)

  # For rebuild rows WAITING_SINCE is the time of the verdict the lister saw.
  def test_a_recheck_that_stayed_red_is_skipped(self):
    """The state is the same before and after, only the time tells."""
    self.assertTrue(stale("failed", "error", "2000", "1000"))

  def test_a_recheck_that_went_green_is_skipped(self):
    self.assertTrue(stale("failed", "success", "2000", "1000"))

  def test_a_green_rebuild_done_since_is_skipped(self):
    self.assertTrue(stale("succeeded", "success", "2000", "1000"))

  def test_a_rebuild_with_no_newer_status_runs(self):
    for build_type, state in (("failed", "error"), ("succeeded", "success")):
      self.assertFalse(stale(build_type, state, "1000", "1000"), build_type)

  def test_it_fails_open_when_anything_is_unknown(self):
    for build_type in ("untested", "failed", "succeeded"):
      self.assertFalse(stale(build_type, ""), build_type)
    for created, since in (("", "1000"), ("2000", ""), ("2000", "0"), ("x", "1")):
      self.assertFalse(stale("failed", "error", created, since), (created, since))


class LatestGithubStateTestCase(unittest.TestCase):
  class FakeGH:
    def __init__(self, statuses):
      self.statuses = statuses

    def get(self, *args, **kwds):
      return self.statuses

  def test_the_newest_status_of_the_context_wins(self):
    """GitHub lists statuses newest first."""
    gh = self.FakeGH([
      {"context": "build/other", "state": "failure"},
      {"context": "build/X", "state": "success", "created_at": "2026-11-10T00:26:22Z"},
      {"context": "build/X", "state": "pending", "created_at": "2026-11-09T15:40:06Z"},
    ])
    self.assertEqual(latestGithubState(gh, "alisw/alidist@abc", "build/X"),
                     ("success", "2026-11-10T00:26:22Z"))

  def test_no_status_for_the_context_is_none(self):
    gh = self.FakeGH([{"context": "build/other", "state": "success"}])
    self.assertIsNone(latestGithubState(gh, "alisw/alidist@abc", "build/X"))

  def test_an_unreadable_status_list_raises(self):
    with self.assertRaises(RuntimeError):
      latestGithubState(self.FakeGH(None), "alisw/alidist@abc", "build/X")


if __name__ == "__main__":
  unittest.main()
