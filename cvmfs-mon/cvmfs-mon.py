#!/usr/bin/env python
from __future__ import print_function
from requests import get
from requests.exceptions import RequestException
import yaml, time, os
from datetime import datetime
from sys import exit
from smtplib import SMTP

def notify(notif, to, **keys):
  if not to:
    print("%s:%s: cannot send notification: no email contact" % (keys["repo"], keys["replica"]))
    return
  subject = notif["subject"] % keys
  body = "Subject: %s\nFrom: %s\nTo: %s\n\n" % (subject, notif["from"], ", ".join(to))
  body += notif["body"] % keys
  if os.environ.get("CVMFSMON_NO_NOTIF", "0") != "0":
    print("%(repo)s:%(replica)s: would send the following email through " \
          "%(host)s:%(port)d:\n---\n%(body)s\n---" %  { "repo": keys["repo"],
                                                        "replica": keys["replica"],
                                                        "host": notif["smtp"]["host"],
                                                        "port": notif["smtp"]["port"],
                                                        "body": body })
    return
  try:
    mailer = SMTP(notif["smtp"]["host"], notif["smtp"]["port"])
    mailer.sendmail(notif["from"], to, body)
  except Exception as e:
    print("%s:%s: cannot send email: %s:%s" % (keys["repo"], keys["replica"], type(e), e))
    return
  print("%s:%s: notification sent to %s" % (keys["repo"], keys["replica"], ", ".join(to)))

def getint(d, key, default):
  v = d.get(key, default)
  try:
    return int(v)
  except ValueError:
    return default

def check(monit):
  for repo in monit["repos"]:
    try:
      api_url = monit["api_url"] + "/" + repo
      s = get(api_url).json()
      ok = s["status"] == "ok"
    except (RequestException,KeyError,ValueError) as e:
      print("%s: cannot get monitoring info from %s: %s:%s" % (repo, api_url, type(e), e))
      continue

    last_modified_stratum0 = datetime.utcfromtimestamp(s["recommendedStratum0"]["publishedTimestamp"])

    for replica in monit["replicas"]:
      # find corresponding stratum1 in API response by equal URLs
      replica_status = next((x for x in s["recommendedStratum1s"]
                             if x["url"] == monit["replicas"][replica]["url"]), None)
      if replica_status == None:
        print("%s:%s cannot found status for stratum 1" % (repo, replica))
        continue

      last_modified = datetime.utcfromtimestamp(replica_status["publishedTimestamp"])
      pub_delta = (datetime.utcnow()-last_modified).total_seconds()
      revdiff = s["recommendedStratum0"]["revision"]-replica_status["revision"]

      if revdiff == 0:
        print("%s:%s: OK" % (repo, replica))
      elif revdiff <= monit["max_revdelta"] and pub_delta <= monit["max_timedelta"]:
        print("%s:%s: syncing: %d seconds, %d revisions behind (stratum0 updated %d seconds ago)" % \
          (repo, replica, pub_delta, revdiff, pub_delta))
      else:
        print("%s:%s: error: %d seconds, %d revisions behind (stratum0 updated %d seconds ago)" % \
          (repo, replica, pub_delta, revdiff, pub_delta))
        if time.time()-monit["last_notification"][repo][replica] > monit["snooze"]:
          notify(monit["notif"],
                 to=monit["replicas"][replica]["contact"],
                 replica=replica,
                 repo=repo,
                 api_url=api_url,
                 delta_rev=revdiff,
                 delta_time=pub_delta,
                 stratum0_mod=last_modified_stratum0.isoformat(),
                 stratum1_mod=last_modified.isoformat(),
                 stratum0_rev=s["recommendedStratum0"]["revision"],
                 stratum1_rev=replica_status["revision"])
          monit["last_notification"][repo][replica] = time.time()

if __name__ == "__main__":
  try:
    monit = yaml.safe_load(open("cvmfs-mon.yml"))
    for replica in monit["replicas"]:
      if "contact" in monit["replicas"][replica]:
        monit["replicas"][replica]["contact"] = monit["replicas"][replica]["contact"].split(",")
      else:
        monit["replicas"][replica]["contact"] = None
  except (yaml.YAMLError,IOError,KeyError) as e:
    print("cannot parse configuration: %s:%s" % (type(e), e))
    exit(1)

  monit["last_notification"] = {}
  for repo in monit["repos"]:
    monit["last_notification"][repo] = {}
    for replica in monit["replicas"]:
      monit["last_notification"][repo][replica] = 0

  monit["notif"] = monit.get("notif", {})
  for k in ["from", "subject", "body", "smtp"]:
    if not k in monit["notif"]:
      monit["notif"] = {}
  if monit["notif"]:
    monit["notif"]["smtp"] = monit["notif"]["smtp"].split(":", 1)
    try:
      monit["notif"]["smtp"].append(int(monit["notif"]["smtp"].pop(1)))
    except (ValueError,IndexError) as e:
       monit["notif"]["smtp"].append(25) # default smtp port
    monit["notif"]["smtp"] = dict(zip(["host", "port"], monit["notif"]["smtp"]))
    print("email notifications will be sent via %(host)s:%(port)d" % monit["notif"]["smtp"])
  else:
    print("email notifications disabled")
    notify = lambda *x, **y: False

  monit["sleep"] = getint(monit, "sleep", 120)
  monit["snooze"] = getint(monit, "snooze", 3600)
  monit["max_timedelta"] = getint(monit, "max_timedelta", 7200)
  monit["max_revdelta"] = getint(monit, "max_revdelta", 7200)

  while True:
    check(monit)
    print("sleeping %d seconds" % monit["sleep"])
    time.sleep(monit["sleep"])
