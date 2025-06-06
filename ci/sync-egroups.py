#!/usr/bin/env python
import yaml
import ldap
import sys

ld = ldap.initialize("ldap://xldap.cern.ch")
def egroup_members(egroup):
  ldap_sid = ld.search("DC=cern,DC=ch",
                       ldap.SCOPE_SUBTREE,
                       "(&(cn=%s)(objectClass=group))" % egroup,
                       None)
  rtype,rdata = ld.result(ldap_sid, 0)
  if rdata and rtype == ldap.RES_SEARCH_ENTRY:
    members = []
    if len(rdata) < 1 or len(rdata[0]) < 2 or "member" not in rdata[0][1]:
      return []
    for m in rdata[0][1]["member"]:
      cn = [ x.encode("latin-1")[3:] for x in m.decode("latin-1").split(",") if x.startswith("CN=") ][0]
      if "OU=Users" in m.decode("latin-1"):
        members.append(cn)
      elif "OU=e-groups" in m.decode("latin-1"):
        members.extend(egroup_members(cn))
    return [ x.decode("latin-1") for x in sorted(list(set(members)))]
  return []

# If egroups are provided as params, print them to stdout and quit
for eg in sys.argv[1:]:
  print("%s: %s" % (eg, " ".join(egroup_members(eg))))
if len(sys.argv) > 1:
  sys.exit(0)

# Open perms.yml: read all groups that are not defined in place
perms = yaml.safe_load(open("perms.yml"))
def_groups = perms.get("groups", {}).keys()  # list of group names
ldap_groups = {}  # dict of group definitions
for repo in perms:
  if repo == "groups": continue
  for rule in perms[repo].get("rules", []):
    for xrule in rule:
      for g in rule[xrule].replace(",", " ").replace("approve=", "").split():
        if g.startswith("@") and g[1:] not in def_groups:
          ldap_groups[g[1:]] = []
      break
  for adm in perms[repo].get("admins", "").split(","):
    if adm and adm.startswith("@") and adm[1:] not in def_groups:
      ldap_groups[adm[1:]] = []

# Open LDAP connection: try to get definitions for each group
for g in ldap_groups:
  ldap_groups[g] = " ".join(egroup_members(g))

# Dump egroups
print(yaml.dump(ldap_groups, default_flow_style=False, width=1000000, indent=2))
