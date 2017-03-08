import unittest
from alibot_helpers.github_utilities import calculateMessageHash
from alibot_helpers.github_utilities import parseGithubRef

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
