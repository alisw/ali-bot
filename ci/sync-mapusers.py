#!/usr/bin/env python
from __future__ import print_function
from requests import get
from random import choice
import yaml,sys
try:
  from urlparse import urlsplit,urlunsplit
except ImportError:
  from urllib.parse import urlsplit,urlunsplit

url = sys.argv[1]
mesos_dns = "leader.mesos:8123"

parts = list(urlsplit(url))
host = parts[1].split(":", 1)[0]
host = "_"+host.replace(".", "._tcp.", 1)

try:
  parts[1] = str(choice([ x["ip"]+":"+x["port"]
                         for x in get("http://%s/v1/services/%s" % (mesos_dns,host)).json()
                         if x.get("ip", None) ]))
  url = urlunsplit(parts)
except Exception:
  sys.stderr.write("Error resolving service for %s\n" % url)

# Update current mapping with new
newmap = { k:v["user_github"]+" "+v["fullname"] if isinstance(v, dict) else v for k,v in get(url).json()["login_mapping"].items() }
umap = yaml.safe_load(open("mapusers.yml"))
umap.update(newmap)
for k in sorted(umap.keys()):
  print("%s: %s" % (k, umap[k]))
