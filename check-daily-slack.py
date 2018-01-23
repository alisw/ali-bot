#!/usr/bin/env python

# check-daily-slack.py -- Check for the daily AliPhysics tag, notify on Slack.
#
# This script continuously checks for the presence of the AliPhysics daily tag
# every 300 seconds, on AliEn and CVMFS. If it's past 4pm it checks for today's
# daily, if it's before, it checks for yesterday's. In case the tag is found on
# both AliEn and CVMFS, a successful notification is sent and no further
# notification will occur (until the next daily tag). In case it's not found,
# a nagging notification is sent every 15 minutes.
#
# Time checks are all explicitly performed using the Geneva time zone, and
# notifications are sent on a certain Slack channel.
#
# Configuration is done via environment variables:
#
# SLACK_PRIVATE_URL
#   The Slack API URL used to write to the configured channel
# SLACK_DEBUG (optional)
#   Set to 1 to print output on stdout (quiet otherwise)
# CVMFS_CHECK_POSIX (optional)
#   Set to 1 to check on mounted /cvmfs or Parrot-provided /cvmfs (by default
#   tries to use the CVMFS monitor HTTP interface)

from __future__ import print_function
import json
import os
import pytz
import re
import requests
import subprocess
import traceback
from datetime import datetime, timedelta
from subprocess import Popen
from sys import exit
from time import sleep

def getout(cmd):
  with open(os.devnull) as dn:
    p = Popen(cmd if type(cmd) is list else cmd.split(" "), stdout=subprocess.PIPE, stderr=dn)
    out = p.communicate()[0]
    code = p.returncode
  return (out,code)

def quench(f):
  def wrap(*x, **y):
    try:
      return f(*x, **y)
    except Exception as e:
      print("%s failed with %s:" % (f.__name__, type(e).__name__))
      traceback.print_exc()
      return False
  return wrap

@quench
def print_slack(msg):
  debug(msg)
  requests.post(os.environ["SLACK_PRIVATE_URL"], data=json.dumps({"text":msg}))
  return True

def debug(msg):
  if not os.environ.get("SLACK_DEBUG", "0").lower() in [ "1", "yes", "true", "on" ]:
    return
  print("%s> %s" % (datetime.isoformat(datetime.utcnow()), msg))

def is_on_cvmfs(pkg, tag):
  if os.environ.get("CVMFS_CHECK_POSIX", "0").lower() in [ "1", "yes", "true", "on" ]:
    return is_on_cvmfs_posix(pkg, tag)
  return is_on_cvmfs_http(pkg, tag)

@quench
def is_on_cvmfs_posix(pkg, tag):
  # Check if found on CVMFS on at least one architecture
  parrot = [ "env",
             "HTTP_PROXY=DIRECT;",
             "PARROT_ALLOW_SWITCHING_CVMFS_REPOSITORIES=yes",
             "PARROT_CVMFS_REPO=<default-repositories>",
             "parrot_run" ]
  for cmdprefix in ([], parrot):
    for d in getout(cmdprefix + ["ls", "-1", "/cvmfs/alice.cern.ch"])[0].split("\n"):
      if getout(cmdprefix + ["bash", "-c", "ls -1 /cvmfs/alice.cern.ch/%s/Modules/modulefiles/%s/%s*" % (d, pkg, ver)])[1] == 0:
        return True
  return False

@quench
def is_on_cvmfs_http(pkg, tag):
  path_prefix = "/cvmfs-monitor/cb/browser/alice.cern.ch/latest"
  url_prefix = "http://cernvm-monitor.cern.ch/%s" % path_prefix
  for i in requests.get(url_prefix, stream=True).iter_lines():
    m = re.search('href="%s/([^"?]+)"' % path_prefix, i)
    if m:
      d = m.group(1).strip("/")
      for j in requests.get("%s/%s/Modules/modulefiles/AliPhysics" % (url_prefix, d), stream=True, timeout=20).iter_lines():
        if tag in j:
          return True
  return False

@quench
def is_on_alien(pkg, tag):
  # Check if found on AliEn from the alimonitor web page
  alien_pkg = "VO_ALICE@%s::%s" % (pkg, tag)
  for line in requests.get("http://alimonitor.cern.ch/packages", stream=True, timeout=20).iter_lines():
    if alien_pkg in line:
      return True
  return False

status_alien = False
status_cvmfs = False
last_ver = ""
last_report_err = 0
while True:
  # Decide what is the tag to verify. Takes correct timezone into account
  now = datetime.now(pytz.timezone("Europe/Zurich"))
  tagtime = now - timedelta(days=1 if now.hour < 16 else 0)
  tagtime = pytz.timezone("Europe/Zurich").localize(datetime(tagtime.year, tagtime.month, tagtime.day, 16))
  elapsed = (now-tagtime).total_seconds()
  ver = "vAN-%04d%02d%02d" % (tagtime.year, tagtime.month, tagtime.day)
  debug("Checking %s" % ver)
  if ver != last_ver:
    # New day, new tag
    status_alien = False
    status_cvmfs = False
    last_ver = ver
    last_report_err = 0
  if not status_alien or not status_cvmfs:
    status_alien = is_on_alien("AliPhysics", ver) if not status_alien else True
    status_cvmfs = is_on_cvmfs("AliPhysics", ver) if not status_cvmfs else True
    if status_alien and status_cvmfs:
      if not print_slack(":+1: %s/%s OK: found both on AliEn and CVMFS" % ("AliPhysics", ver)):
        last_ver = ""  # force resend
    elif elapsed > 5400 and (last_report_err == 0 or (now-last_report_err).total_seconds() > 900):
      # Start worrying after 17h30 (+5400 s after 16h). Snooze for 15 minutes (900 s)
      if not print_slack((":poop: %s/%s not OK: "  +
                          "%savailable on AliEn, " +
                          "%savailable on CVMFS")  % ("AliPhysics", ver,
                                                      "" if status_alien else "not ",
                                                      "" if status_cvmfs else "not ")):
        last_ver = ""  # force resend
      last_report_err = now - timedelta(days=0)
    else:
      debug("Not OK (AliEn: %s, CVMFS: %s) but within grace time" % (status_alien, status_cvmfs))
  else:
    debug("Already verified to be OK")
  sleep(300)
