#!/usr/bin/env python
from __future__ import print_function
from collections import OrderedDict
from hashlib import sha1
import pickle

import sys
import re

def printStats(gh):
  print("Github API used %s/%s" % gh.rate_limiting, file=sys.stderr)

# Anything which can resemble an hash or a date is filtered out.
def calculateMessageHash(message):
  return sha1("\n".join(sorted(re.sub("[0-9a-f-A-F]", "", message).split("\n")))).hexdigest()[0:10]

VALID_STATES = ["pending", "success", "error", "failure"]

def loadCommits():
  message = ""
  try:
    with open(".cached-commits", "r+") as f:
      return pickle.load(f)
  except IOError as e:
    pass
  except EOFError as e:
    message = "Malformed cache file"
  except pickle.PickleError as e:
    message = "Could not read commit cache"
  message and print(message, file=sys.stderr)
  return OrderedDict()

# Dumps the cache. Since commits is an OrderedDict we can limit it to
# the last 1000 insertions.
def dumpCommits(commits):
  message = ""
  try:
    with open(".cached-commits", "w") as f:
      pickle.dump(OrderedDict(commits.items()[-1000:]), f, 2)
  except IOError as e:
    message = "Unable to write cache"
  except EOFError as e:
    message = "Malformed cache file"
  except pickle.PickleError as e:
    message = "Could not write commit cache"
  print(message, file=sys.stderr)

def setGithubStatus(gh, args, cache={}):
  repo_name = args.commit.split("@")[0]
  commit_ref = args.commit.split("@")[1] if "@" in args.commit else "master"
  state_context = args.status.rsplit("/", 1)[0] if "/" in args.status else ""
  state_value = args.status.rsplit("/", 1)[1] if "/" in args.status else args.status
  print(state_value, state_context)
  if not state_value in VALID_STATES:
    raise RuntimeError("Valid states are " + ",".join(VALID_STATES))

  repo = gh.get_repo(repo_name)
  commit = cache.get(commit_ref) or repo.get_commit(commit_ref)
  cache[commit_ref] = commit

  # Avoid creating a new state if the previous one is exactly the same.
  for s in commit.get_statuses():
    # If the state already exists and it's different, create a new one
    if s.context == state_context and (s.state != state_value or s.target_url != args.url or s.description != args.message):
      print("Last status for %s does not match. Updating." % state_context, file=sys.stderr)
      printStats(gh)
      commit.create_status(state_value, args.url, args.message, state_context)
      return
    # If the state already exists and it's teh same, exit
    if s.context == state_context and s.state == state_value and s.target_url == args.url and s.description == args.message:
      print("Last status for %s is already matching. Exiting" % state_context, file=sys.stderr)
      printStats(gh)
      return
  # If the state does not exists, create it.
  print("%s does not exist. Creating." % state_context, file=sys.stderr)
  commit.create_status(state_value, args.url, args.message, state_context)
