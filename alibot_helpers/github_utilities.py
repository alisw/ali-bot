#!/usr/bin/env python
from __future__ import print_function
from collections import OrderedDict
from hashlib import sha1
import inspect
import os
import json
import pickle
import re
import sys

import requests

from alibot_helpers.utilities import to_unicode

def trace(func):
    """Simple function to trace enter and exit of a function/method
    and the args/kws that were passed. It is activated by -d/--debug
    being present in the sys.argv list, otherwise just returns the
    un-decorated function.
    """
    sysargs = sys.argv[1:]
    if not ('--debug' in sysargs or '-d' in sysargs):
        return func

    def _(txt=func.__name__, enter=True):
        prefix = "==> " if enter else "<== "
        return '{0} {1}\n'.format(prefix, txt)

    def examine(d):
        m = ''
        for i, (k, v) in enumerate(d.items(), 1):
            m += '  [{0}] {1}({2}), value: {3}\n'.format(i, k, type(v), str(v))
        return m

    def wrapped(*args, **kws):
        argspec = inspect.getargspec(func)
        path = None
        if argspec.args and argspec.args[0] == 'self':
            # this func is actually an instance method
            inst = args[0]
            path = inst.__module__ 
            path += '.' + inst.__class__.__name__ 
            path += ':' + func.__name__ + '():'
        else:
            path = func.__module__ + '.' + func.__name__ + '():'
        
        m = _(path)
    
        if args:
            m += '{0} *args:\n'.format(len(args))
            m += examine(OrderedDict(zip(argspec.args, args)))

        if kws:
            m += '{0} **kws:\n'.format(len(kws))
            m += examine(kws)

        print(m)
        try:
            retval = func(*args, **kws)
            print(_(path, enter=False))
        except:
            print('Exception caught, re-raising.')
            print(_(path, enter=False))
            raise
        else:
            return retval
    return wrapped


def github_token():
    try:
        return os.environ["GITHUB_TOKEN"]
    except KeyError:
        raise RuntimeError("GITHUB_TOKEN env var not found, please set it")


def generateCacheId(entries):
    h = sha1()
    for k, v in entries:
        h.update(k.encode("ascii"))
        h.update(v.encode("ascii"))
    return h.hexdigest()


def parseLinks(linkString):
    """Parses the Link header string and gets the url for the next page.
    If the next page is not found, returns None.
    """
    if not linkString:
        return None

    links = linkString.split(",")
    for x in links:
        url, what = x.split(";")
        if what.strip().startswith("rel=\"next\""):
            sanitized = url.strip().strip("<>")
            return sanitized


def pagination(cache_item, nextLink, api, self, stable_api):
    for x in cache_item["payload"]:
        yield x
    if nextLink:
        for x in self.get(nextLink.replace(api, ""), stable_api):
            yield x


class PickledCache(object):
    def __init__(self, filename):
        self.filename = filename
        self.cache = OrderedDict()

    def __enter__(self):
        self.load()
        return self

    def __exit__(self, excType, excValue, tb):
        self.dump()
        return False

    def update(self, d):
        self.cache.update(d)

    def load(self):
        message = ""
        try:
            with open(self.filename, "rb+") as f:
                self.cache = pickle.load(f)
                return
        except IOError:
            pass
        except EOFError:
            message = "Malformed cache file %s" % self.filename
        except pickle.PickleError:
            message = "Could not read commit cache %s" % self.filename
        except:
            message = "Generic error while decoding %s" % self.filename

        message and print(message, file=sys.stderr)
        self.cache = OrderedDict()

    def dump(self, limit=1000):
        message = ""
        try:
            with open(self.filename, "wb") as f:
                pickle.dump(OrderedDict(list(self.cache.items())[-limit:]), f, 2)
        except IOError:
            message = "Unable to write cache file %s" % self.filename
        except EOFError:
            message = "Malformed cache file %s" % self.filename
        except pickle.PickleError:
            message = "Could not write to cache %s" % self.filename

        print(message, file=sys.stderr)

    def __getitem__(self, key):
        return self.cache.get(key, {})

    def __delitem__(self, key):
        try:
            del self.cache[key]
        except KeyError:
            pass


class GithubCachedClient(object):
    def __init__(self, token, cache, api="https://api.github.com"):
        self.token = token
        self.api = api
        self.cache = cache
        self.printStats()

    def __enter__(self):
        self.cache.load()
        return self

    def __exit__(self, excType, excValue, traceback):
        self.cache.dump()
        self.printStats()
        return False

    @property
    def rate_limiting(self):
        """Get the Github rate limit: requests allowed, left and when
        the quota will be reset.
        """
        url = self.makeURL("/rate_limit")
        response = requests.get(url=url, headers=self.baseHeaders())
        limits = (-1, -1)
        if response.status_code == 200:
            headers = response.headers
            remaining = int(headers.get("X-RateLimit-Remaining", -1))
            limit = int(headers.get("X-RateLimit-Limit", -1))
            limits = (remaining, limit)
        return limits

    def printStats(self):
        print("Github API used %s/%s" % self.rate_limiting, file=sys.stderr)

    def makeURL(self, template, **kwds):
        template = template[1:] if template.startswith('/') else template
        return os.path.join(self.api, template.format(**kwds))

    def baseHeaders(self, stable_api=True):
        stableAPI = "application/vnd.github.v3+json"
        unstableAPI = "application/vnd.github.shadow-cat-preview+json"
        headers = {
            "Accept": stableAPI if stable_api else unstableAPI,
            "Authorization": "token %s" % self.token.strip()
        }
        return headers

    def getHeaders(self, stable_api=True, etag=None, lastModified=None):
        headers = self.baseHeaders(stable_api)
        if etag:
            headers.update({"If-None-Match": etag})
        if lastModified:
            headers.update({"If-Modified-Since": lastModified})
        return headers

    def postHeaders(self, stable_api=True):
        return self.baseHeaders(stable_api)

    @trace
    def post(self, url, data, stable_api=True, **kwds):
        headers = self.postHeaders(stable_api)
        url = self.makeURL(url, **kwds)
        data = json.dumps(data) if type(data) == dict else data
        response = requests.post(url=url, data=data, headers=headers)
        sc = response.status_code
        return sc

    @trace
    def patch(self, url, data, stable_api=True, **kwds):
        headers = self.postHeaders(stable_api)
        url = self.makeURL(url, **kwds)
        data = json.dumps(data) if type(data) == dict else data
        response = requests.patch(url=url, data=data, headers=headers)
        return response.status_code

    @trace
    def get(self, url, stable_api=True, **kwds):
        # If we have a cache getter we use it to obtain an
        # entry in the cachedcache_item etags
        cacheKey = generateCacheId([("url", url)] + list(kwds.items()))
        cacheValue = self.cache[cacheKey]
        headers = self.getHeaders(stable_api,
                                  cacheValue.get("ETag"),
                                  cacheValue.get("Last-Modified"))

        url = self.makeURL(url, **kwds)
        # final_url = "{s.api}{url}".format(s=self, url=url).format(**kwds)
        r = requests.get(url=url, headers=headers)

        if r.status_code == 304:
            if type(cacheValue["payload"]) == list:
                nextLink = parseLinks(cacheValue.get("Link"))
                return pagination(cacheValue,
                                nextLink,
                                self.api,
                                self,
                                stable_api)
            return cacheValue["payload"]

        # If we are here, it means we had some sort of cache miss.
        # Therefore we pop the cacheHash from the cache.
        del self.cache[cacheKey]

        if r.status_code == 404:
            return None

        if r.status_code == 403:
            print("Forbidden", file=sys.stderr)
            return None

        if r.status_code == 200:
            cacheValue = {
                "payload": r.json(),
                "ETag": r.headers.get("ETag"),
                "Last-Modified": r.headers.get("Last-Modified"),
                "Link": r.headers.get("Link")
            }
            self.cache.update({cacheKey: cacheValue})
            if type(cacheValue["payload"]) == list:
                nextLink = parseLinks(cacheValue["Link"])
                return pagination(cacheValue,
                                  nextLink,
                                  self.api,
                                  self,
                                  stable_api)
            return cacheValue["payload"]

        if r.status_code == 204:
            cacheValue = {
                "payload": True,
                "ETag": r.headers.get("ETag"),
                "Last-Modified": r.headers.get("Last-Modified")
            }
            self.cache.update({cacheKey: cacheValue})
            return cacheValue["payload"]

        print(r.status_code)
        assert(False)


def calculateMessageHash(message):
    # Anything which can resemble a hash or a date is filtered out.
    subbed = re.sub("[0-9a-f-A-F]", "", to_unicode(message))
    sortedSubbed = sorted(subbed.split("\n"))
    sha = sha1("\n".join(sortedSubbed).encode("ascii", "ignore"))
    return sha.hexdigest()[0:10]


def parseGithubRef(s):
    repo_name = re.split("[@#]", s)[0]
    commit_ref = s.split("@")[1] if "@" in s else "master"
    pr_n = re.split("[@#]", s)[1] if "#" in s else None
    return (repo_name, pr_n, commit_ref)

def setGithubStatus(cgh, args):
    repo_name, _, commit_ref = parseGithubRef(args.commit)
    state_context = args.status.rsplit("/", 1)[0] if "/" in args.status else ""
    state_value = args.status.rsplit("/", 1)[1] if "/" in args.status else args.status
    print(state_value, state_context)

    VALID_STATES = ["pending", "success", "error", "failure"]
    if state_value not in VALID_STATES:
        raise RuntimeError("Valid states are " + ",".join(VALID_STATES))

    all_statuses = cgh.get("/repos/{repo_name}/statuses/{ref}",
                           repo_name=repo_name,
                           ref=commit_ref)
    for s in all_statuses:
        # If the state already exists and it's different, create a new one
        if (s["context"] == state_context and
            (s["state"] != state_value or
             s["target_url"] != args.url or
             s["description"] != args.message)):
            print(s)
            print("Last status for %s does not match. Updating." % state_context, file=sys.stderr)
            print(cgh.rate_limiting) 

            data = {
                "state": state_value,
                "context": state_context,
                "description": args.message,
                "target_url": args.url
            }
            cgh.post("/repos/{repo_name}/statuses/{ref}",
                     data=data,
                     repo_name=repo_name,
                     ref=commit_ref)
            return

        # If the state already exists and it's the same, exit
        if (s["context"] == state_context and
            s["state"] == state_value and
            s["target_url"] == args.url and
            s["description"] == args.message):
            msg = "Last status for %s is already matching. Exiting" % state_context
            print(msg, file=sys.stderr)
            cgh.printStats()
            return

    # If the state does not exists, create it.
    print("%s does not exist. Creating." % state_context, file=sys.stderr)
    data = {
        "state": state_value,
        "context": state_context,
        "description": args.message,
        "target_url": args.url
    }
    cgh.post(
        "/repos/{repo_name}/statuses/{ref}",
        data=data,
        repo_name=repo_name,
        ref=commit_ref
    )
