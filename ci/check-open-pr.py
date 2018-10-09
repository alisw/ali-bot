#!/usr/bin/env python

# check-open-pr.py
#
# Usage:
#  check-open-pr.py 16:00 alisw/AliPhysics build/AliPhysics/release build/AliPhysics/root6
#
# The command returns nonzero if we still need to wait for open pull requests. It returns zero if
# we do not need to wait any longer.

from __future__ import print_function
from github import Github,GithubException
import os, sys, pytz
from datetime import datetime
import json

def utc_to_local(utc_dt):
  tz = pytz.timezone("Europe/Zurich")
  dt = utc_dt.replace(tzinfo=pytz.utc).astimezone(tz)
  return tz.normalize(dt) # .normalize might be unnecessary

# Arguments
deadline = sys.argv[1]
repo_name = sys.argv[2]
checks = sys.argv[3:]
if not checks:
  print("No check specified, aborting")
  exit(1)

# Tag at 4pm today, Geneva time
tag_time = utc_to_local(datetime.utcnow())
tag_time = tag_time.replace(microsecond=0, second=0, **dict(zip(["hour", "minute"], map(int, deadline.split(":", 1)))))

# Read GitHub token; fallback on environment if not found in the given file
try:
  github_token = open(os.path.expanduser("~/.github-token")).read().strip()
except:
  github_token = os.environ["GITHUB_TOKEN"]

gh = Github(login_or_token=github_token)

print("Threshold: %s (Geneva time), checks to wait for: %s" % (tag_time, ", ".join(checks)))
for pull in gh.get_repo(repo_name).get_pulls():

  # Check time: PR must have been opened before the time threshold
  when = utc_to_local(pull.created_at)
  if when > tag_time:
    print("%s#%d: created at %s (Geneva): not waiting: too late" % (repo_name, pull.number, when))
    continue

  review = None
  build = {}
  wait_for_build = True
  what_approved = None

  # Collect statuses (review and required build checks). Beware, states are
  # cumulative (state history is kept). We should only keep the first one per
  # given context, the others are old!
  for st in pull.base.repo.get_commit(pull.head.sha).get_statuses():
    if st.context == "review" and not review:
      review = st.state
      what_approved = st.description
    if st.context in checks and not st.context in build:
      # This is one of the mandatory tests
      build[st.context] = st.state
      if st.state == "error":
        wait_for_build = False

  # Take a decision. If we need to wait, exit from the program with nonzero
  # Policy: we need all PRs to be:
  #  * opened before the deadline,
  #  * review == "success" with status == "merge approved" (not just testing)
  #  * all build statuses must NOT be "error"
  wait_for_build = (wait_for_build and review == "success" and what_approved == "merge approved")

  # Print out the current status
  decision = "must wait" if wait_for_build else "not waiting: bad state"
  print(("{repo}#{prnum}: created at {ctime} (Geneva): review={review}, " +
         "what_approved={what_approved}, checks={checks}: {decision}").format(
    repo=repo_name,
    prnum=pull.number,
    ctime=when,
    review=review,
    what_approved=what_approved,
    checks=json.dumps(build),
    decision=decision))

  if wait_for_build:
    # Stop immediately; save GitHub API calls, we do not need to check the other PRs
    print("Cannot proceed with tagging")
    sys.exit(1)

print("May proceed with tagging")
sys.exit(0)
