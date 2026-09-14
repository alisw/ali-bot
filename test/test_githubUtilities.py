import os
import unittest
from unittest.mock import patch
from alibot_helpers.github_utilities import calculateMessageHash
from alibot_helpers.github_utilities import parseGithubRef
from alibot_helpers.github_utilities import GithubCachedClient
from alibot_helpers.github_utilities import relativeLink
from alibot_helpers.github_utilities import setGithubStatus, StatusLimitReached


class TestAuthorizationHeader(unittest.TestCase):
  """The REST auth form depends on who we are talking to.

  GitHub accepts both "token <x>" and "Bearer <x>", so the direct path keeps the
  historical spelling. A credential broker accepts only Bearer -- it matches
  that header to decide which gate token to swap for the real one -- and answers
  401 otherwise. The failure is invisible at the call site: it surfaces as
  NotImplementedError(401) out of handle_pr_id(), naming neither auth nor the
  proxy, which is what kept slc10's reporting dry-run.
  """

  def headerFor(self, api_url):
    env = dict(os.environ)
    env.pop("GITHUB_API_URL", None)
    if api_url is not None:
      env["GITHUB_API_URL"] = api_url
    client = GithubCachedClient.__new__(GithubCachedClient)
    client.token = "SECRET"
    with patch.dict(os.environ, env, clear=True):
      return client.baseHeaders()["Authorization"]

  def test_direct_github_keeps_the_historical_token_form(self):
    self.assertEqual(self.headerFor(None), "token SECRET")

  def test_a_broker_gets_bearer(self):
    self.assertEqual(self.headerFor("http://127.0.0.1:9999/github"), "Bearer SECRET")

  def test_the_token_is_not_mangled(self):
    """Whichever branch runs, the credential itself must be passed through
    verbatim -- a stripped or re-encoded token fails as a 401 too."""
    for url in (None, "http://127.0.0.1:9999/github"):
      self.assertTrue(self.headerFor(url).endswith(" SECRET"))


class TestPaginationLinks(unittest.TestCase):
    """Following a Link header must work through a credential broker too.

    GitHub names the UPSTREAM host in its Link header even when we reached it
    via a broker, so the old `nextLink.replace(api, "")` matched nothing and the
    follow-up request went straight to api.github.com with a gate token it will
    not accept. get() then returned None and setGithubStatus died with
    "TypeError: 'NoneType' object is not iterable" -- which named neither
    pagination nor the proxy.

    It was broken against GitHub directly as well: stripping the base left a
    leading slash, and makeURL()'s os.path.join() discards its first argument
    when the second is absolute. Only commits with enough statuses to paginate
    reach this code, which is how it went unnoticed.
    """

    LINK = "https://api.github.com/repositories/123/statuses?page=2"

    def test_the_result_joins_onto_either_base(self):
        rel = relativeLink(self.LINK)
        for base in ("https://api.github.com", "http://127.0.0.1:9/github"):
            self.assertEqual(os.path.join(base, rel),
                             base + "/repositories/123/statuses?page=2")

    def test_no_leading_slash(self):
        """os.path.join() throws the base away if this starts with '/'."""
        self.assertFalse(relativeLink(self.LINK).startswith("/"))

    def test_the_query_survives(self):
        """Losing it re-requests page 1 for ever."""
        self.assertIn("page=2", relativeLink(self.LINK))
        self.assertIn("per_page=100", relativeLink(
            "https://api.github.com/repos/a/b/pulls?per_page=100&page=3"))


class TestGithubHelpers(unittest.TestCase):
  def test_messageHash(self):
    self.assertEqual(calculateMessageHash("foo"), calculateMessageHash("foo"))
    self.assertEqual(calculateMessageHash("fofsanjcn 00:00:00"), calculateMessageHash("fofsanjcn 10:21:10"))
    self.assertEqual(calculateMessageHash("deadbeef0123456789DEADBEEF"), calculateMessageHash("deadbaaf"))
    self.assertNotEqual(calculateMessageHash("fofsonjcn 00:00:00"), calculateMessageHash("fofsanjcn 10:21:10"))
  
  def test_parseGithubRef(self):
    self.assertEqual(parseGithubRef("foo/bar@4787895789324784"), ("foo/bar", None, "4787895789324784"))
    self.assertEqual(parseGithubRef("foo/bar#100@4787895789324784"), ("foo/bar", "100", "4787895789324784"))
    self.assertEqual(parseGithubRef("foo/bar#100"), ("foo/bar", "100", "master"))

if __name__ == '__main__':
    unittest.main()


class TestStatusLimit(unittest.TestCase):
  """A 422 on a status POST is terminal, and must not be swallowed.

  GitHub caps statuses per (sha, context). Once a pair is full the POST returns
  422 and nothing can ever be written for it again -- so the check keeps the
  value it last held, the queue keeps offering it as `failed`, and a worker
  rebuilds it forever. That is not hypothetical: alidist#6245 was unwritable
  from 2026-08-17 and consumed 2000 of 5344 rounds across the fleet, 37% of all
  CI capacity, because post() printed the 422 and every caller ignored the
  returned code.
  """

  class FakeGH:
    """An existing status to update, plus a scripted POST status code."""

    def __init__(self, post_code, existing=True):
      self.post_code, self.posts, self.existing = post_code, 0, existing
      self.rate_limiting = (5000, 5000)

    def get(self, *args, **kwds):
      if not self.existing:
        return []
      return [{"context": "build/X", "state": "error",
               "target_url": "", "description": "old"}]

    def post(self, *args, **kwds):
      self.posts += 1
      return self.post_code

    def printStats(self):
      pass

  class Args:
    commit = "alisw/alidist@deadbeef"
    status = "build/X/error"
    message = "Rechecking since now"
    url = ""
    keep_url = False

  def test_422_updating_an_existing_status_raises(self):
    gh = self.FakeGH(422)
    with self.assertRaises(StatusLimitReached):
      setGithubStatus(gh, self.Args(), debug_print=False)
    self.assertEqual(gh.posts, 1, "should raise only after attempting the POST")

  def test_422_creating_a_new_status_raises(self):
    gh = self.FakeGH(422, existing=False)
    with self.assertRaises(StatusLimitReached):
      setGithubStatus(gh, self.Args(), debug_print=False)

  def test_it_names_the_context_that_can_no_longer_be_written(self):
    gh = self.FakeGH(422)
    with self.assertRaises(StatusLimitReached) as caught:
      setGithubStatus(gh, self.Args(), debug_print=False)
    self.assertIn("build/X", str(caught.exception))

  def test_it_is_a_RuntimeError_so_existing_handlers_still_catch_it(self):
    """set-github-status distinguishes it for the exit code, but anything that
    only knows about RuntimeError must keep working."""
    self.assertTrue(issubclass(StatusLimitReached, RuntimeError))

  def test_a_successful_post_still_returns_normally(self):
    gh = self.FakeGH(201)
    setGithubStatus(gh, self.Args(), debug_print=False)
    self.assertEqual(gh.posts, 1)

  def test_a_matching_status_posts_nothing(self):
    """What keeps an unchanged check from spending quota on every pass -- the
    same quota whose exhaustion causes the loop above."""
    gh = self.FakeGH(201)

    class Same(TestStatusLimit.Args):
      message = "old"

    setGithubStatus(gh, Same(), debug_print=False)
    self.assertEqual(gh.posts, 0)
