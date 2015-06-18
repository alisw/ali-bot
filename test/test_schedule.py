from unittest import TestCase
from schedule import decide
from sys import path
class RuleDecisionTests(TestCase):
    def setUp(self):
      self.releases = ["v5-02-26", "v4-02-25"]
    def test_exclude_all(self):
      result = decide([], self.releases, [{"exclude": ".*"}])
      self.assertEqual(result, [], "Excluding all not empty.")
      result = decide([], self.releases, [{"exclude": ".*"}, {"include": ".*"}])
      self.assertEqual(result, [], "Excluding all not empty.")
      result = decide([], self.releases, [{"include": ".*", "architecture": "someArch"}, {"exclude": ".*"}])
      self.assertEqual(len(result), 2, "Including all not empty")

    def test_nothing_matches(self):
      result = decide([], self.releases, [{"include": "v2.*", "architecture": "someArch"}])
      self.assertEqual(result, [], "Should not match anything")

if __name__ == '__main__':
  path.append(".")
  unittest.main()
