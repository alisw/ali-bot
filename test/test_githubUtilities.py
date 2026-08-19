import os
import unittest
from unittest.mock import patch
from alibot_helpers.github_utilities import calculateMessageHash
from alibot_helpers.github_utilities import parseGithubRef
from alibot_helpers.github_utilities import GithubCachedClient
from alibot_helpers.github_utilities import relativeLink


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
