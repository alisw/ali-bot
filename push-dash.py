#!/usr/bin/env python
from __future__ import print_function
import os
import re
import requests, urllib3
import subprocess
import logging
import traceback
from os.path import expanduser
from tempfile import mkdtemp
from collections import deque
from datetime import datetime, timedelta
from subprocess import Popen
from sys import exit
from time import sleep
from base64 import b64decode
from ci.metagit import MetaGit,MetaGitException

class InfluxDBStreamer():
  def __init__(self, baseurl):
    if baseurl.startswith("insecure_https:"):
      self.ssl_verify = False
      self.baseurl = baseurl[9:]
    else:
      self.ssl_verify = True
      self.baseurl = baseurl
    if "#" in self.baseurl:
      self.baseurl,self.database = self.baseurl.split("#", 1)
    else:
      self.database = ""
    self.buffer = deque("", 20)
    self.log = logging.getLogger("influxdb")
    self.log.setLevel(logging.DEBUG if os.environ.get("MON_DEBUG", "0") == "1" else logging.INFO)

  def __call__(self, series, tags, fields):
    if not fields:
      self.log.debug("Nothing to send, skipping")
      return
    # Line protocol: https://docs.influxdata.com/influxdb/v1.0/write_protocols/line_protocol_tutorial/
    fields = dict(map(lambda (k,v): (k, '"%s"'%v if isinstance(v, basestring) else v), fields.iteritems()))
    data_string = series + "," +                                              \
                  ",".join(["%s=%s" % (x,tags[x]) for x in tags]) + " " +     \
                  ",".join(["%s=%s" % (x,fields[x]) for x in fields]) + " " + \
                  str(int((datetime.utcnow()-datetime.utcfromtimestamp(0)).total_seconds()*1000000000))
    self.buffer.append(data_string)
    self.log.debug("Appended (%d in queue now): %s" % (len(self.buffer), data_string))

  def dump(self):
    self.log.debug("Dumping:\n---\n%s\n---" % "\n".join(self.buffer))
    try:
      r = requests.post(self.baseurl+"/write",
                        headers={ "Content-type": "application/octet-stream",
                                  "Accept": "text/plain" },
                        params={ "db": self.database },
                        data="\n".join(self.buffer).encode("utf-8"),
                        timeout=5,
                        verify=self.ssl_verify)
      r.raise_for_status()
      self.buffer.clear()
      return True
    except requests.exceptions.RequestException as e:
      self.log.error("Error sending data: %s" % e)
      return False

def quench(f):
  def wrap(*x, **y):
    try:
      return f(*x, **y)
    except Exception as e:
      log.error("%s failed with %s:" % (f.__name__, type(e).__name__))
      traceback.print_exc()
      return None
  return wrap

def getout(cmd):
  with open(os.devnull) as dn:
    p = Popen(cmd if type(cmd) is list else cmd.split(" "), stdout=subprocess.PIPE, stderr=dn)
    out = p.communicate()[0]
    code = p.returncode
  return (out,code)

@quench
def getdf(host, vol):
  out,rc = getout("ssh -F /dev/null -oStrictHostKeyChecking=no -oPreferredAuthentications=publickey {keys} {host} df -k {vol} | tail -n1".format(keys=ssh_keys_opt, host=host, vol=vol))
  if rc != 0:
    raise Exception("Command returned an error: %d" % rc)
  return dict(zip([ "used", "avail" ],
                  [ int(x) for x in re.split("\s+", out)[2:4] ]))

@quench
def getgithubreposize(repo):
  global git
  return git.get_repo_info(repo).size

if __name__ == "__main__":

  logging.basicConfig()
  logging.getLogger("requests").setLevel(logging.WARNING)
  urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

  log = logging.getLogger("monitor")
  log.setLevel(logging.DEBUG if os.environ.get("MON_DEBUG", "0") == "1" else logging.INFO)
  git = None

  try:
    pause = int(os.environ["MON_PAUSE"])
  except:
    pause = 600

  # Initialize from environment
  try:
    mon = InfluxDBStreamer(os.environ["MON_INFLUX_URL"])
  except KeyError:
    log.fatal("You should set MON_INFLUX_URL to something like \"insecure_https://<USER>:<PASS>@dbod-hltmon.cern.ch:8082#<TABLE>\"")
    exit(1)
  try:
    git = MetaGit.init(backend="GitHub", token=os.environ["MON_GITHUB_TOKEN"])
  except KeyError:
    log.fatal("You should set MON_GITHUB_TOKEN to a valid GitHub token")
    exit(1)

  count = 1
  ssh_key_dir = mkdtemp()
  ssh_keys_opt = ""
  while True:
    varname = "MON_SSH_KEY_%d" % count
    if not varname in os.environ:
      break
    fn = os.path.join(ssh_key_dir, "key_%d" % count)
    fd = os.open(fn, os.O_CREAT|os.O_WRONLY, 0600)
    log.debug("Writing SSH key to %s" % fn)
    os.write(fd, b64decode(os.environ[varname]))
    os.close(fd)
    ssh_keys_opt += " -i" + fn
    count = count+1
  ssh_keys_opt = ssh_keys_opt.strip()
  del fn
  del fd

  while True:
    # Monitoring loop

    # Relevant storage
    mon(series="storage", tags={"storagename": "repo"}, fields=getdf("root@alibuild03", "/build/reports/repo"))
    mon(series="storage", tags={"storagename": "jenkins"}, fields=getdf("root@alijenkins01", "/var/lib/jenkins"))
    mon(series="storage", tags={"storagename": "macpromat"}, fields=getdf("alibuild@macpromat", "/build"))
    mon(series="storage", tags={"storagename": "alimacx06"}, fields=getdf("admin@alimacx06", "/build"))

    # GitHub repository size
    for repo in ("alisw/AliPhysics", "alisw/AliRoot", "AliceO2Group/AliceO2"):
      mon(series="repo", tags={"reponame": repo}, fields={"size": getgithubreposize(repo)})

    mon.dump()
    log.debug("Sleeping %d seconds" % pause)
    sleep(pause)
