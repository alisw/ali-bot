import unittest
from alibot_helpers.github_utilities import calculateMessageHash

class TestGithubHelpers(unittest.TestCase):
  def test_messageHash(self):
    self.assertEqual(calculateMessageHash("foo"), calculateMessageHash("foo"))
    self.assertEqual(calculateMessageHash("fofsanjcn 00:00:00"), calculateMessageHash("fofsanjcn 10:21:10"))
    self.assertEqual(calculateMessageHash("deadbeef0123456789DEADBEEF"), calculateMessageHash("deadbaaf"))
    self.assertNotEqual(calculateMessageHash("fofsonjcn 00:00:00"), calculateMessageHash("fofsanjcn 10:21:10"))


if __name__ == '__main__':
    unittest.main()
