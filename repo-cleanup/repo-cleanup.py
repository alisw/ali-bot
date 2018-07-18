#!/usr/bin/env python

from __future__ import print_function
from datetime import datetime, timedelta
from collections import OrderedDict
from operator import itemgetter
from tempfile import mkdtemp
import os
import subprocess
import re
import os.path
import sys
import yaml
import json
import logging

class ColorLogFormatter(logging.Formatter):
  # This class helps printing log messages with colored labels
  def __init__(self, fmt):
    self.fmt = fmt
    self.COLOR_RESET = "\033[m" if sys.stdout.isatty() else ""
    self.LEVEL_COLORS = { logging.WARNING:  "\033[4;33m",
                          logging.ERROR:    "\033[4;31m",
                          logging.CRITICAL: "\033[1;37;41m",
                          logging.INFO:     "\033[4;34m",
                          logging.DEBUG:    "\033[1;4;1m" } if sys.stdout.isatty() else {}

  def to_unicode(self, s):
    if sys.version_info[0] >= 3:
      if isinstance(s, bytes):
        return s.decode("utf-8")  # to get newlines as such and not as escaped \n
      return str(s)
    elif isinstance(s, str):
      return unicode(s, "utf-8")  # utf-8 is a safe assumption
    elif not isinstance(s, unicode):
      return unicode(str(s))
    return s

  def format(self, record):
    record.msg = self.to_unicode(record.msg)
    return "\n".join([ self.fmt %
                       { "levelname": self.LEVEL_COLORS.get(record.levelno, self.COLOR_RESET) +
                                      record.levelname +
                                      self.COLOR_RESET,
                         "message": x }
                       for x in record.msg.split("\n") ])

# Set logging
logger = logging.getLogger(__name__)
lh = logging.StreamHandler()
lh.setFormatter(ColorLogFormatter("%(levelname)s: %(message)s"))
logger.addHandler(lh)
logger.setLevel(logging.DEBUG)

# Define convenience functions for logging
def critical(m):
  logger.critical(m)
  sys.exit(1)
def error(m): logger.error(m)
def warning(m): logger.warning(m)
def info(m): logger.info(m)
def debug(m): logger.debug(m)

# Use the most optimal way to iterate over key,value pairs according to Python
if sys.version_info.major >= 3:
  def it(d): return d.items()
else:
  def it(d): return d.iteritems()

# Create a CSV file with the reference count report. The file is readable in Excel, for instance
def refCountToCsv(refCount, fn):
  with open(fn, "w") as w:
    countZero = 0
    count = 0
    w.write("architecture;package;version;refcount;sizebytes;sizemb;humansize;cdateutc;sha\n")
    for arch in refCount:
      for pkgVer,pkgDef in it(refCount[arch]):
        size = humanSize(pkgDef["size"])
        cdate = datetime.fromtimestamp(pkgDef["creation"])
        count += 1
        if pkgDef["refcount"] == 0:
          countZero += 1
        w.write("{arch};{pkg};{ver};{refcount};{sizebytes};{sizemb};{hsize};{cdate};{sha}\n".format(
          arch=arch, pkg=pkgDef["name"], ver=pkgDef["ver"], refcount=pkgDef["refcount"],
          sizebytes=size["bytes"], sizemb=int(size["bytes"]/1000000), hsize=size["human"],
          cdate=cdate, sha=pkgDef["sha"]))
  info("Written {file}. {countzero} out of {count} have zero refcount".format(
    file=fn, countzero=countZero, count=count))

# Return a stringified version of a package definition
def pkgDefToText(pkgDef):
  return "{name} {ver}, created on {cdate}, {hsize}".format(
           name=pkgDef["name"], ver=pkgDef["ver"],
           cdate=datetime.fromtimestamp(pkgDef["creation"]),
           hsize=humanSize(pkgDef["size"])["human"])

# Convert bytes into a human-readable size
def humanSize(sz):
  bsz = sz
  um = ""
  for u in "kMGT":
    if sz > 1000:
      sz = float(sz)/1000.
      um = u
  return { "bytes": bsz,
           "human": str(round(sz, 2)) + " " + um + "B",
           "unit": um + "B",
           "sizeUnit": sz }

# Parse a package file name, return pkgname,ver
def parsePackage(fn, arch, packages, ext=".tar.gz"):
  end = ".%s%s" % (arch,ext)
  if not fn.endswith(end):
    return None,None
  fn = fn[:-len(end)]

  pkg = None
  for p in packages:
    if fn.startswith(p):
      pkg = p
      break
  if pkg is None:
    return None,None
  ver = fn[len(pkg)+1:]
  return pkg,ver

def execute(command):
  popen = subprocess.Popen(command, shell=False, stdout=subprocess.PIPE)
  lines_iterator = iter(popen.stdout.readline, "")
  for line in lines_iterator:
    if not line: break
    debug(line.strip("\n")) # yield line
  out = popen.communicate()[0]
  if out:
    debug(out.strip("\n"))
  return popen.returncode

def main():
  # Load and parse rules
  rawRules = yaml.safe_load(open("repo-cleanup.yml").read())
  conf = rawRules.get("repo_cleanup", {})
  if not isinstance(conf, dict):
    critical("Config: repo_cleanup configuration must be a dictionary")
  if "repo_cleanup" in rawRules:
    del rawRules["repo_cleanup"]
  rules = {}
  for pkgName,pkgRules in it(rawRules):
    if not isinstance(pkgName, basestring):
      critical("Config: package name {pkg} must be a string".format(pkg=pkgName))
    if not isinstance(pkgRules, list):
      critical("Config: there must be a list of rules for package {pkg}".format(pkg=pkgName))
    for rule in pkgRules:
      if not isinstance(rule, dict) or len(rule) != 1:
        critical("Config: for package {pkg}: list of one-item dicts expected".format(pkg=pkgName))
      pkgReRaw = rule.keys()[0]
      try:
        pkgRe = re.compile(pkgReRaw)
      except:
        critical("Config: regexp {re} for {pkg} is invalid".format(pkg=pkgName, re=pkgReRaw))
      # We now have package and version regexp (validated): look for rules
      purgeOlderThan = rule[pkgReRaw].get("purgeOlderThan", -1)
      excludeFromPurge = rule[pkgReRaw].get("excludeFromPurge", None)
      # Validate purgeOlderThan
      if purgeOlderThan == "never":
        purgeOlderThan = -1
      try:
        purgeOlderThan = int(purgeOlderThan)
      except:
        critical("Config: in {pkg}, {re}: not a number of days for purgeOlderThan: {pot}".format(
          pkg=pkgName, re=pkgReRaw, pot=purgeOlderThan))
      # Validate excludeFromPurge
      if excludeFromPurge is not None and excludeFromPurge not in [ "firstOfMonth", "lastOfMonth" ]:
        critical(("Config: in {pkg}, {re}: excludeFromPurge must be \"firstOfMonth\" or " +
                  "\"lastOfMonth\": {efp}").format(pkg=pkgName, re=pkgReRaw, efp=excludeFromPurge))
      # Validation passed: form new dictionary item
      debug("Config: pkg={pkg} regexp={re} purgeOlderThan={pot} excludeFromPurge={efp}".format(
        pkg=pkgName, re=pkgReRaw, pot=purgeOlderThan, efp=excludeFromPurge))
      rules[pkgName] = rules.get(pkgName, []) + \
        [ { pkgRe: { "purgeOlderThan": purgeOlderThan,
                     "excludeFromPurge": excludeFromPurge } } ]

  del rawRules
  debug("Config: " + str(conf))
  debug("Rules: " + str(rules))
  info("Toplevel packages: %s" % ", ".join(rules.keys()))

  packages = set()  # list of unique packages
  refCount = {}     # refcount for each package (per arch)

  # Execute report script: this is a lengthy operation
  current_path = os.path.dirname(os.path.realpath(__file__))
  temp_results =  mkdtemp()
  info("Temporary working directory generated: {path}".format(path=temp_results))
  os.environ["TARBALLS_PREFIX"] = conf["tarballs_prefix"]
  os.environ["TEMP_RESULTS"] = temp_results
  info("Generating repository usage report (this might take a while)")
  rc = execute([ os.path.join(current_path, "gen-repo-report.sh") ])
  if rc != 0:
    critical("Repository report script exited with {code}".format(code=rc))

  # We open the report generated by the previous step
  f = open(os.path.join(temp_results, "repo-report.txt"))

  # List of packages
  info("Determining list of packages...")
  for line in f:
    line = line.strip()
    if line.startswith("dist "):

      # Line is: "dist" arch package
      _,arch,path = line.split(" ", 2)
      path = os.path.normpath(path)
      pkg,_ = path.split("/", 1)

      # Lazy population of architectures
      if not arch in refCount:
        refCount[arch] = {}
      packages.add(pkg)

  # Unique packages, sorted with longest name first
  packages = list(packages)
  packages.sort(lambda x,y: cmp(len(y), len(x)))

  # Prepare reference counts
  info("Initialising reference counts...")
  f.seek(0)
  for line in f:
    line = line.strip()
    if not line.startswith("store "):
      continue
    _,arch,size,creation,path = line.strip().split(" ", 4)

    sha = os.path.basename(os.path.dirname(path))
    path = os.path.basename(path)

    # Extract package name and version from the filename (also performs checks)
    pkg,ver = parsePackage(path, arch, packages)
    if not pkg:
      warning("Skipping line: %s" % line)
      continue
    # Note! ALL packages from the store have an entry here. We will use refcount later to determine
    # what packages to purge.
    p = { "size": int(size),
          "creation": int(creation),
          "ver": ver,
          "sha": sha,
          "name": pkg,
          "refcount": 0 }
    refCount[arch][pkg + "-" + ver] = p  # Key is PkgName-PkgVer

  # Purge/keep toplevel packages by policy. If a package has to be kept, its
  # refcount is set to 1
  info("Applying purging rules to the toplevel packages...")
  nowUtc = datetime.utcnow()
  considerKeepLast = {}   # used in lastOfMonth
  considerKeepFirst = {}  # used in firstOfMonth
  toplevel = rules.keys()
  for arch in refCount:
    considerKeepLast[arch] = {}
    considerKeepFirst[arch] = {}
    for t in toplevel:
      considerKeepFirst[arch][t] = {}
      considerKeepLast[arch][t] = {}
    for pkgVer,pkgDef in it(refCount[arch]):
      if pkgDef["name"] in toplevel:
        matched = False
        for pkgRule in rules[pkgDef["name"]]:
          # pkgRule is a one-val dict with the regexp and the actions
          for pkgRe,pkgAction in it(pkgRule):
            pass
          if pkgRe.search(pkgDef["ver"]):
            purgeOlderThan = pkgAction["purgeOlderThan"]
            excludeFromPurge = pkgAction["excludeFromPurge"]
            #debug("Apply: {pkgVer} matches: purgeOlderThan={pot} excludeFromPurge={efp}".format(
            #  pkgVer=pkgVer, pot=purgeOlderThan, efp=excludeFromPurge))
            matched = True
            if purgeOlderThan < 0:
              # Case: [nopurgeExplicit]
              debug("Match: [nopurgeExplicit] {pkgVer} will be kept".format(pkgVer=pkgVer))
              pkgDef["refcount"] = 1
            else:
              delta = nowUtc - datetime.fromtimestamp(pkgDef["creation"])
              delta = delta.total_seconds() / 86400  # days
              if delta > purgeOlderThan:
                if excludeFromPurge is None:
                  # Case: [purgeWillHappen]
                  debug("Match: [purgeWillHappen] {pkgVer} will be purged".format(pkgVer=pkgVer))
                  pkgDef["refcount"] = 0
                else:
                  # Case: [purgeConsidered]
                  # We have to match a string corresponding to a date: YYYYMMDD for the moment
                  m = re.search("(2[0-9]{3})(0[1-9]|1[012])([012][0-9]|3[012])", pkgDef["ver"])
                  if m:
                    yearMonth = int(m.group(1) + m.group(2))
                    dayOfMonth = int(m.group(3))
                    debug("Match: [purgeConsidered] {pkgVer} considered ({ym}, {dom})".format(
                      pkgVer=pkgVer, ym=yearMonth, dom=dayOfMonth))
                    pkgDef.update({ "dom": dayOfMonth })
                    if excludeFromPurge == "firstOfMonth":
                      considerKeepFirst[arch][pkgDef["name"]][yearMonth] = \
                        considerKeepFirst[arch][pkgDef["name"]].get(yearMonth, []) + [pkgDef]
                    elif excludeFromPurge == "lastOfMonth":
                      considerKeepLast[arch][pkgDef["name"]][yearMonth] = \
                        considerKeepLast[arch][pkgDef["name"]].get(yearMonth, []) + [pkgDef]
                    else:
                      assert False, "BUG: only firstOfMonth and lastOfMonth should be here"
                  else:
                    warning(("Match: {pkgVer} considered for purging, but no date-like " +
                             "string (YYYYMMDD) found in version: keeping").format(pkgVer=pkgVer))
                    pkgDef["refcount"] = 1
              else:
                # Case: [nopurgeTooYoung]
                debug("Match: [nopurgeTooYoung] {pkgVer} is young: keeping".format(pkgVer=pkgVer))
                pkgDef["refcount"] = 1
            break
        if not matched:
          debug("Match: {pkgVer} has no matches: keeping by default".format(pkgVer=pkgVer))
          pkgDef["refcount"] = 1

  refCountToCsv(refCount, os.path.join(temp_results, "report-match.csv"))

  # Sort consider* lists and prefill condemned with matching toplevel packages
  listCount = 0
  for consider in [ considerKeepFirst, considerKeepLast ]:
    for arch,pkgs in it(consider):
      for pkg,yearMonths in it(pkgs):
        for yearMonth,pkgList in it(yearMonths):
          pkgList.sort(key=lambda x: x["dom"])
          if len(pkgList) == 0:
            continue
          assert listCount == 0 or listCount == 1, "BUG: only two lists expected"
          pkgDef = pkgList[0] if listCount == 0 else pkgList[-1]
          del pkgDef["dom"]
          refCount[arch][pkgDef["name"]+"-"+pkgDef["ver"]]["refcount"] = 1  # keep
          #debug("Consider: keep {pkg} {ver} ({arch}) in {yearmonth}".format(
          #  pkg=pkgDef["name"], ver=pkgDef["ver"], arch=arch, yearmonth=yearMonth))
    listCount += 1
  del considerKeepLast
  del considerKeepFirst
  del listCount

  refCountToCsv(refCount, os.path.join(temp_results, "report-consider.csv"))

  # Compute reference counts
  info("Calculating reference counts...")
  f.seek(0)
  for line in f:
    line = line.strip()
    if line.startswith("dist "):

      # Line is: "dist" arch package
      _,arch,path = line.split(" ", 2)
      path = os.path.normpath(path)
      pkg,pkgVer,fn = path.split("/", 2)
      dep,depVer = parsePackage(fn, arch, packages)
      depKey = dep + "-" + depVer
      pkgDef = refCount[arch].get(pkgVer)

      if pkgDef is None:
        warning("RefCount: toplevel {pkg} ({arch}) no longer in store, skipping".format(
          pkg=pkgVer, arch=arch))
      elif dep is None:
        warning("RefCount: invalid dependency, skipping %s" % line)
      elif not depKey in refCount[arch]:
        warning("RefCount: dependency {dep} ({arch}) no longer in store, skipping".format(
          dep=depKey, arch=arch))
      elif depKey != pkgVer and pkg in toplevel and pkgDef["refcount"] > 0:
        # Increment reference counts for toplevel packages kept by policies. This is why we check if
        # the toplevel package has refcount > 0 already. We also deal with the case of packages
        # depending on themselves.
        # In practice: this dependency is required if the toplevel package requiring it is kept.
        refCount[arch][depKey]["refcount"] += 1

  f.close()

  # Simple rule: at this point, every package with refcount == 0 is not needed. All toplevel
  # packages to be kept by policy had refcount incremented already. For the moment we generate a
  # script to be run for cleaning the storage up effectively
  script = open(os.path.join(temp_results, "run-cleanup.sh"), "w")
  script.write("#!/bin/bash\n")
  totalSizePurged = 0
  totalPurged = 0
  dryrun = os.environ.get("DRYRUN", "")
  dryrun = False if not dryrun or dryrun == "0" else True
  for arch in refCount:
    for pkgVer,pkgDef in it(refCount[arch]):
      if pkgDef["refcount"] == 0:
        totalSizePurged += pkgDef["size"]
        totalPurged += 1
        info("{arch} {pkgdef}".format(arch=arch, pkgdef=pkgDefToText(pkgDef)))
        script.write("{dry}rm -fv /build/reports/repo/TARS/{arch}/store/{ssha}/{sha}/{name}-{ver}.{arch}.tar.gz\n".format(
          dry="echo DRYRUN " if dryrun else "",
          arch=arch, sha=pkgDef["sha"], ssha=pkgDef["sha"][0:2],
          name=pkgDef["name"], ver=pkgDef["ver"]))
  script.close()

  refCountToCsv(refCount, os.path.join(temp_results, "report-refcount.csv"))

  # Effectively running the cleanup script
  if dryrun:
    warning("Running effective cleanup in dry run mode (not deleting files!)")
  rc = execute([ "bash", os.path.join(temp_results, "run-cleanup.sh") ])
  if rc != 0:
    critical("Cleanup script exited with {code}".format(code=rc))

  info("Total size saved: {size}, packages to remove: {removed}".format(
    size=humanSize(totalSizePurged)["human"], removed=totalPurged))

main()
