#!/usr/bin/env python
from __future__ import print_function
import yaml, sys

groups = {}
repos = {}

for fn in sys.argv[1:]:
  fn,gh = fn.split(":", 1)
  repos[gh] = { "tests": ["build/%s/release"%gh.split("/",1)[1]] }
  repos[gh]["rules"] = []
  with open(fn) as f:
    branch = "master"
    for l in f:
      l = l.strip()
      if l.startswith("@") and "=" in l:
        # This is a group definition
        g,m = map(str.strip, l.split("="))
        g = g[1:]
        m = " ".join(sorted(set([ x for x in m.split(" ") if x ])))
        groups[g] = {"members": m, "count": groups[g]["count"] if g in groups else 0 }
      elif l.startswith("RW"):
        try:
          what,who = l.split("=", 1)
          what = what.split()[1]
          who = [ x for x in who.strip().split() if x ]
        except IndexError:
          continue
        if what.startswith("VREF/NAME/"):
          if branch != "master":
            continue
          what = "^%s/.*$" % what[10:]
          for w in who:
            if w.startswith("@"):
              try:
                groups[w[1:]]["count"] = groups[w[1:]]["count"] + 1
              except KeyError as e:
                continue
          repos[gh]["rules"].append({ what: " ".join(who) })
        else:
          branch = what.strip("$")

# Ditch unused groups
groups = dict([ (x,groups[x]["members"]) for x in groups if groups[x]["count"] ])

repos.update({"groups":groups})
print(yaml.dump(repos, default_flow_style=False, width=1000000, indent=2))
