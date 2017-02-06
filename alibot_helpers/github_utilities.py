#!/usr/bin/env python
from __future__ import print_function

from hashlib import sha1

import sys
import re

def printStats(gh):
  print("Github API used %s/%s" % gh.rate_limiting, file=sys.stderr)

# Anything which can resemble an hash or a date is filtered out.
def calculateMessageHash(message):
  return sha1("\n".join(sorted(re.sub("[0-9a-f-A-F]", "", message).split("\n")))).hexdigest()[0:10]
