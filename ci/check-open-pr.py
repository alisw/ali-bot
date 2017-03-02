#!/usr/bin/env python
from __future__ import print_function
from github import Github,GithubException
import os, sys, pytz
from datetime import datetime

def utc_to_local(utc_dt):
  tz = pytz.timezone("Europe/Zurich")
  dt = utc_dt.replace(tzinfo=pytz.utc).astimezone(tz)
  return tz.normalize(dt) # .normalize might be unnecessary

# Tag at 4pm today, Geneva time
tag_time = utc_to_local(datetime.utcnow())
tag_time = tag_time.replace(microsecond=0, second=0, **dict(zip(["hour", "minute"], map(int, sys.argv[1].split(":", 1)))))

gh = Github(login_or_token=open(os.path.expanduser("~/.github-token")).read().strip())

for repo_name in sys.argv[2:]:
  build_test_name = "build/%s/release" % (repo_name.split("/", 1)[1] if "/" in repo_name else repo_name)
  print("Threshold: %s (Geneva), build test name: %s" % (tag_time, build_test_name))
  for pull in gh.get_repo(repo_name).get_pulls():
    when = utc_to_local(pull.created_at)
    if when > tag_time:
      print("%s#%d: created at %s (Geneva): not waiting: too late" % (repo_name, pull.number, when))
      continue
    # review must be success / build/AliPhysics/release must not be error
    review = None
    build = None
    what_approved = None
    for st in pull.base.repo.get_commit(pull.head.sha).get_statuses():
      if not build and st.context == build_test_name:
        build = st.state
      if not review and st.context == "review":
        review = st.state
        what_approved = st.description
        print(what_approved)
      if review and build:
        break
    if review == "success" and not build == "error" and what_approved == "merge approved":
      print("%s#%d: created at %s (Geneva), review: %s (%s), build: %s: must wait" % (repo_name, pull.number, when, review, what_approved, build))
      sys.exit(1)
    else:
      print("%s#%d: created at %s (Geneva), review: %s (%s), build: %s: not waiting: bad state" % (repo_name, pull.number, when, review, what_approved, build))

print("May proceed with tagging")
sys.exit(0)
