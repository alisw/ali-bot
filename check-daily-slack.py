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
import os.path
import pytz
import re
import requests
import subprocess
import traceback
from datetime import datetime, timedelta
from subprocess import Popen
from sys import exit
from time import sleep

class CvmfsHttp(object):
  def _split_path(self, path):
    m = re.search('^/+cvmfs/+([^/]+)/*(.*)$', path)
    if m:
      sub = os.path.normpath(m.group(2))
      sub = "" if sub == "." else sub
      return ("/cvmfs-monitor/cb/browser/%s/latest" % m.group(1),
              "/cvmfs-monitor/cb/document/%s/latest" % m.group(1),
              sub)
    else:
      raise Exception("Invalid CVMFS path")
  def _get_url_prefix(self):
    return "http://cernvm-monitor.cern.ch"
  def ls(self, path):
    http_prefix,doc_prefix,path = self._split_path(path.rstrip("/"))
    path_prefix = os.path.join(http_prefix, path)
    doc_prefix = os.path.join(doc_prefix, path)
    url_prefix = self._get_url_prefix() + path_prefix
    lst = []
    for i in requests.get(url_prefix, stream=True).iter_lines():
      md = re.search('href="%s/+([^"?]+)"' % path_prefix.replace('/', '/+'), i)
      if md:
        lst.append(md.group(1).strip("/"))
      else:
        mf = re.search('href="%s/+([^"?]+)"' % doc_prefix.replace('/', '/+'), i)
        if mf:
          lst.append(mf.group(1).strip("/"))
    return lst
  def exists(self, path):
    path = path.rstrip("/")
    _,_,sub = self._split_path(path)
    if not sub:
      return True
    return os.path.basename(path) in self.ls(os.path.dirname(path))

class CvmfsPosix(object):
  def ls(self, path):
    parrot = [ "env",
               "HTTP_PROXY=DIRECT;",
               "PARROT_ALLOW_SWITCHING_CVMFS_REPOSITORIES=yes",
               "PARROT_CVMFS_REPO=<default-repositories>",
               "parrot_run" ]
    lst = []
    for cmdprefix in ([], parrot):
      for d in getout(cmdprefix + ["ls", "-1", path])[0].split("\n"):
        if "LibCvmfs" in d or not d:
          continue
        lst.append(d)
    return list(set(lst))
  def exists(self, path):
    return os.path.basename(path) in self.ls(os.path.dirname(path))

def debug(msg):
  if not os.environ.get("SLACK_DEBUG", "0").lower() in [ "1", "yes", "true", "on" ]:
    return
  print("%s> %s" % (datetime.isoformat(datetime.utcnow()), msg))

if os.environ.get("CVMFS_CHECK_POSIX", "0").lower() in [ "1", "yes", "true", "on" ]:
  debug("Using CVMFS POSIX interface")
  cvmfs = CvmfsPosix()
else:
  debug("Using CVMFS HTTP interface")
  cvmfs = CvmfsHttp()

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

@quench
def is_on_cvmfs(pkg, tag):
  for d in cvmfs.ls("/cvmfs/alice.cern.ch"):
    for p in cvmfs.ls(os.path.join("/cvmfs/alice.cern.ch", d , "Modules/modulefiles/%s" % pkg)):
      if p.startswith(ver):
        return True
  return False

def update_health_check():
  with open("/tmp/check-daily-slack-health", "w") as hc:
    hc.write("healthy")

@quench
def is_on_alien(pkg, tag):
  # Check if found on AliEn from the alimonitor web page
  alien_pkg = "VO_ALICE@%s::%s" % (pkg, tag)
  for line in requests.get("http://alimonitor.cern.ch/packages", stream=True, timeout=20).iter_lines():
    if alien_pkg in line:
      return True
  return False

@quench
def is_on_data(tag):
  m = re.search("^vAN-([0-9]{4})", tag)
  if not m:
    raise Exception("Invalid tag format (vAN-YYYYMMDD expected): %s" % tag)
  year = m.group(1)
  return cvmfs.exists("/cvmfs/alice.cern.ch/data/analysis/%s/%s" % (year, tag))

def get_emoji(b):
  return ":white_check_mark:" if b else ":x:"

status_alien = False
status_cvmfs = False
status_data  = False
last_ver = ""
last_report_err = 0
while True:
  # Decide what is the tag to verify. Takes correct timezone into account
  now = datetime.now(pytz.timezone("Europe/Zurich"))
  tagtime = now - timedelta(days=1 if now.hour < 16 else 0)
  #tagtime = now - timedelta(days=5)
  tagtime = pytz.timezone("Europe/Zurich").localize(datetime(tagtime.year, tagtime.month, tagtime.day, 16))
  elapsed = (now-tagtime).total_seconds()
  ver = "vAN-%04d%02d%02d" % (tagtime.year, tagtime.month, tagtime.day)
  debug("Checking %s" % ver)
  if ver != last_ver:
    # New day, new tag
    status_alien = False
    status_cvmfs = False
    status_data  = False
    last_ver = ver
    last_report_err = 0
  if not status_alien or not status_cvmfs or not status_data:
    status_alien = is_on_alien("AliPhysics", ver) if not status_alien else True
    status_cvmfs = is_on_cvmfs("AliPhysics", ver) if not status_cvmfs else True
    status_data  = is_on_data(ver)                if not status_data  else True
    msg = "%s AliEn / %s CVMFS / %s OADB sync" % (get_emoji(status_alien),
                                                  get_emoji(status_cvmfs),
                                                  get_emoji(status_data))
    if status_alien and status_cvmfs and status_data:
      if not print_slack(":+1: %s/%s OK: %s" % ("AliPhysics", ver, msg)):
        last_ver = ""  # force resend
    elif elapsed > 5400 and (last_report_err == 0 or (now-last_report_err).total_seconds() > 900):
      # Start worrying after 17h30 (+5400 s after 16h). Snooze for 15 minutes (900 s)
      if not print_slack(":poop: %s/%s not OK: %s" % ("AliPhysics", ver, msg)):
        last_ver = ""  # force resend
      last_report_err = now - timedelta(days=0)
    else:
      debug("Not OK (AliEn: %s, CVMFS: %s, OADB sync: %s) but within grace time" % \
            (status_alien, status_cvmfs, status_data))
  else:
    debug("Already verified to be OK")
  for _ in range(5):
    update_health_check()
    sleep(60)
