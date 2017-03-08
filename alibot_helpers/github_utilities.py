#!/usr/bin/env python
from __future__ import print_function
from collections import OrderedDict
import requests
from hashlib import sha1
import pickle

import sys
import re

def generateCacheId(entries):
  h = sha1()
  for k,v in entries:
    h.update(k)
    h.update(str(v))
  return h.hexdigest()

# Parses the Link header string and gets the url for the next page. If
# the next page is not found, returns None
def parseLinks(linkString):
  if not linkString:
    return None
  links = linkString.split(",")
  for x in links:
    url, what = x.split(";")
    if what.strip().startswith("rel=\"next\""):
      sanitized = url.strip().strip("<>")
      return sanitized
  return None

def pagination(cache_item, nextLink, api, self, stable_api):
  for x in cache_item["payload"]:
    yield x
  if nextLink:
    for x in self.request("GET", nextLink.replace(api,""), stable_api):
      yield x

class GithubCachedClient(object):
  def __init__(self, token, api="https://api.github.com"):
    self.token = token
    self.api = api
    self.cache = OrderedDict()

  def loadCache(self, filename):
    message = ""
    try:
      with open(filename, "r+") as f:
        self.cache = pickle.load(f)
        return
    except IOError as e:
      pass
    except EOFError as e:
      message = "Malformed cache file"
    except pickle.PickleError as e:
      message = "Could not read commit cache"
    message and print(message, file=sys.stderr)
    self.cache = OrderedDict()
    return

  def dumpCache(self, filename, limit=1000):
    message = ""
    try:
      with open(filename, "w") as f:
        pickle.dump(OrderedDict(self.cache.items()[-limit:]), f, 2)
    except IOError as e:
      message = "Unable to write cache file %s" % filename
    except EOFError as e:
      message = "Malformed cache file %s" % filename
    except pickle.PickleError as e:
      message = "Could not write to cache %s" % filename
    print(message, file=sys.stderr)

  def request(self, method, url, stable_api=True, **kwds):
    #if not "&page=" in url:
    #  url += "&page=1"
    # If we have a cache getter we use it to obtain an
    # entry in the cached etags
    cacheHash = generateCacheId([("url", url)] + kwds.items())
    cache_item = self.cache.get(cacheHash)

    headers = {"Accept": "application/vnd.github.v3+json" if stable_api else "application/vnd.github.korra-preview",
               "Authorization": "token %s" % self.token}
    if cache_item and cache_item.get("ETag"):
      headers.update({"If-None-Match": cache_item["ETag"]})

    if cache_item and cache_item.get("Last-Modified"):
      headers.update({"If-Modified-Since": cache_item["Last-Modified"]})
    final_url = "{s.api}{url}".format(s=self, url=url).format(**kwds)
    r = requests.request(method=method, url=final_url, headers=headers)
    if r.status_code == 304:
      if type(cache_item["payload"]) == list:
        nextLink = parseLinks(cache_item.get("Link"))
        return pagination(cache_item, nextLink, self.api, self, stable_api)
      return cache_item["payload"]
    # If we are here, it means we had some sort of cache miss. Therefore
    # we pop the cacheHash from the cache.
    try:
      del self.cache[cacheHash]
    except:
      pass
    if r.status_code == 404:
      return None
    if r.status_code == 403:
      print("Forbidden", file=sys.stderr)
      return None
    if r.status_code == 200:
      cache_item = {
                    "payload": r.json(),
                    "ETag": r.headers.get("ETag"),
                    "Last-Modified": r.headers.get("Last-Modified"),
                    "Link": r.headers.get("Link")
                  }
      self.cache.update({cacheHash: cache_item})
      if type(cache_item["payload"]) == list:
        nextLink = parseLinks(cache_item["Link"])
        return pagination(cache_item, nextLink, self.api, self, stable_api)
      return cache_item["payload"]
    if r.status_code == 204:
      cache_item = {
                    "payload": True,
                    "ETag": r.headers.get("ETag"),
                    "Last-Modified": r.headers.get("Last-Modified")
                  }
      self.cache.update({cacheHash: cache_item})
      return cache_item["payload"]
    print(r.status_code)
    assert(False)

def printStats(gh):
  print("Github API used %s/%s" % gh.rate_limiting, file=sys.stderr)

# Anything which can resemble an hash or a date is filtered out.
def calculateMessageHash(message):
  return sha1("\n".join(sorted(re.sub("[0-9a-f-A-F]", "", message).split("\n")))).hexdigest()[0:10]

VALID_STATES = ["pending", "success", "error", "failure"]

def loadCommits():
  message = ""
  try:
    with open(".cached-commits", "r+") as f:
      return pickle.load(f)
  except IOError as e:
    pass
  except EOFError as e:
    message = "Malformed cache file"
  except pickle.PickleError as e:
    message = "Could not read commit cache"
  message and print(message, file=sys.stderr)
  return OrderedDict()

# Dumps the cache. Since commits is an OrderedDict we can limit it to
# the last 1000 insertions.
def dumpCommits(commits):
  message = ""
  try:
    with open(".cached-commits", "w") as f:
      pickle.dump(OrderedDict(commits.items()[-1000:]), f, 2)
  except IOError as e:
    message = "Unable to write cache"
  except EOFError as e:
    message = "Malformed cache file"
  except pickle.PickleError as e:
    message = "Could not write commit cache"
  print(message, file=sys.stderr)

def parseGithubRef(s):
  repo_name = re.split("[@#]", s)[0]
  commit_ref = s.split("@")[1] if "@" in s else "master"
  pr_n = re.split("[@#]", s)[1] if "#" in s else None
  return (repo_name, pr_n, commit_ref)

def setGithubStatus(gh, args, cache={}):
  repo_name, _ , commit_ref = parseGithubRef(args.commit)
  state_context = args.status.rsplit("/", 1)[0] if "/" in args.status else ""
  state_value = args.status.rsplit("/", 1)[1] if "/" in args.status else args.status
  print(state_value, state_context)
  if not state_value in VALID_STATES:
    raise RuntimeError("Valid states are " + ",".join(VALID_STATES))

  repo = gh.get_repo(repo_name)
  commit = cache.get(commit_ref) or repo.get_commit(commit_ref)
  cache[commit_ref] = commit

  # Avoid creating a new state if the previous one is exactly the same.
  for s in commit.get_statuses():
    # If the state already exists and it's different, create a new one
    if s.context == state_context and (s.state != state_value or s.target_url != args.url or s.description != args.message):
      print("Last status for %s does not match. Updating." % state_context, file=sys.stderr)
      printStats(gh)
      commit.create_status(state_value, args.url, args.message, state_context)
      return
    # If the state already exists and it's teh same, exit
    if s.context == state_context and s.state == state_value and s.target_url == args.url and s.description == args.message:
      print("Last status for %s is already matching. Exiting" % state_context, file=sys.stderr)
      printStats(gh)
      return
  # If the state does not exists, create it.
  print("%s does not exist. Creating." % state_context, file=sys.stderr)
  commit.create_status(state_value, args.url, args.message, state_context)
