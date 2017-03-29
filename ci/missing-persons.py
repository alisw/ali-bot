#!/usr/bin/env python
from __future__ import print_function
import yaml,sys
email = (len(sys.argv) > 1 and sys.argv[1] == '--email')
cern_users = set()
for g,members in yaml.safe_load(open("groups.yml")).iteritems():
  cern_users.update(members.split())
for ucern in yaml.safe_load(open("mapusers.yml")):
  cern_users.discard(ucern)
cern_users.difference_update(["alibot", "alibrary", "alibuild"])
cern_users = sorted(cern_users)
if email:
  print(", ".join(map(lambda x: x+"@cern.ch", cern_users)))
else:
  for u in cern_users:
    print(u)
