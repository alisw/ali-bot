#!/usr/bin/env python
#
# Helper script which given a alisw repository and a alisw.github.io decides
# which builds to schedule based on a set of rules found in config.yaml. 
#
# Things are done this way so that the process of starting a new build is
# always expressed as moving from one state where some builds are missing to a
# state where all the builds that can be built are built. This is different
# from the past approach where people were asking for "please build a new
# release", this is more "build all releases which are missing".
#
# This script is meant to be executed  by jenkins and will output a series of
# property files which can be to drive the actual builds.
import yaml
from argparse import ArgumentParser
from os.path import exists, basename
from os import makedirs, symlink, unlink
from sys import exit
from commands import getstatusoutput
import re
import logging
from logging import debug
from glob import glob

def format(s, **kwds):
  return s % kwds

def decide_releases(done_tags, all_tags, rules, package):
  to_be_processed = []
  for tag in all_tags:
    if tag in done_tags:
      print "Tag %s was already processed. Skipping" % tag
      continue
    for rule in rules:
      if rule["name"] != package:
        continue
      if "exclude" in rule:
        m = re.match(rule["exclude"], tag)
        if m:
          debug("%s excluded by %s" % (tag, rule["exclude"]))
          break
      elif "include" in rule:
        m = re.match(rule["include"], tag)
        payload = {
           "tag": tag,
           "architecture": rule["architecture"],
           "package": package
        }
        if not m:
          continue
        if str(payload) in done_tags:
          continue
        to_be_processed.append(payload)
      else:
        print "Bad rule found. Exiting"
        exit(1)
  return to_be_processed

def extractTuples(name):
  possible_archs = ["(.*)(slc.*_x86-64)-(.*)[.]ini",
                    "(.*)(ubt.*_x86-64)-(.*)[.]ini",
                    "(.*)(osx.*_x86-64)-(.*)[.]ini"]
  for attempt in possible_archs:
    m = re.match(attempt, name)
    if not m:
      continue
    result = dict(zip(["package", "architecture", "tag"], m.groups()))
    result["package"] = result.get("package", "aliroot").strip("-")
    return result

if __name__ == "__main__":
  parser = ArgumentParser()
  parser.add_argument("--aliroot-repo", dest="aliroot",
                      help="Location of aliroot checkout area")
  parser.add_argument("--debug", dest="debug", action="store_true",
                      help="Print debug output")
  parser.add_argument("--aliphysics-repo", dest="aliphysics",
                      help="Location of aliphysics checkout area")
  parser.add_argument("--alisw-repo", dest="alisw",
                      help="Location of alisw.github.io checkout area")
  parser.add_argument("--dry-run", "-n", action="store_true", dest="dryRun",
                      default=False,
                      help="Just print out what you will do. Do not execute.")
  args = parser.parse_args()

  logger = logging.getLogger()
  logger_handler = logging.StreamHandler()
  logger.addHandler(logger_handler)

  if args.debug:
    logger.setLevel(logging.DEBUG)
    logger_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
  else:
    logger.setLevel(logging.INFO)

  if not args.aliroot or not exists(args.aliroot):
    parser.error("Please specify where to find aliroot")
  if not args.aliphysics or not exists(args.aliphysics):
    parser.error("Please specify where to find aliphysics")
  if not args.alisw or not exists(args.alisw):
    parser.error("Please specify where to find alisw.github.io")

  repoInfos = [ { "repo": args.aliroot, "name": "AliRoot"},
                { "repo": args.aliphysics, "name": "AliPhysics"} ]
  specs = []
  for repoInfo in repoInfos:
    cmd = format("(cd %(repo)s && git tag 2>&1)",
                 repo=repoInfo["repo"])
    err, out = getstatusoutput(cmd)
    if err:
      print "Unable to get %s tags" % repoInfo["name"]
      print out
      exit(1)

    # There are three entries in the script:
    #
    # done_tags: the tags which have already been scheduled for build.
    # all_tags: all the tags available
    # rules: the rules which specify which tags need to be scheduled and using
    #        which version of the recipes.
    done_tags = glob(format("%(alisw)s/data/scheduled/%(name)s-*.ini",
                     alisw=args.alisw, name=repoInfo["name"]))
    done_tags = set(str(extractTuples(basename(x))) for x in done_tags)
    print done_tags
    all_tags = out.split("\n")
    config = yaml.load(file("config.yaml"))
    rules = config["release_rules"]

    # Decide which releases need to be built.
    specs += decide_releases(done_tags, all_tags, rules, repoInfo["name"])
    # Now we process the integration build part. We simply create the ini files
    # for each one of them.
    ibs = config["integration_rules"]
    for ib in ibs:
      payload = {
        "tag": ib["branch"],
        "architecture": ib["architecture"],
        "package": ib["package"]
      }
      specs.append(payload)
  
  if not specs:
    print "No tags to be processed"
    exit(0)
  print "Tags to be scheduled:\n" + "\n".join(str(x) for x in specs)

  if args.dryRun:
    print "Dry run specified, not running."
    exit(0)

  # We create the spec in a subdirectory "specs", so that we can always know
  # what were the options used to build a given release. We then copy them in
  # the local area, so that jenkins can use it to schedule other jobs.
  getstatusoutput("rm -fr *.ini")
  getstatusoutput("mkdir -p %s/data/scheduled" % args.alisw)
  for s in specs:
    p = format("%(alisw)s/data/scheduled/%(package)s-%(architecture)s-%(tag)s.ini",
               alisw=args.alisw,
               architecture=s["architecture"],
               package=s["package"],
               tag=s["tag"])
    f = file(p, "w")
    f.write("ARCHITECTURE=%s\n" % s["architecture"])
    f.write("OVERRIDE_TAGS=%s=%s\n" % (s["package"], s["tag"]))
    f.write("PACKAGE_NAME=%s\n" % s["package"])
    f.close()
    symlink(p, basename(p))
