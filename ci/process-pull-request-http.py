#!/usr/bin/env python

from logging import debug, info, warning, error
from argparse import ArgumentParser
from os.path import expanduser
import logging, re, json, yaml
from klein import Klein
from twisted.internet.task import LoopingCall
from twisted.internet import defer, task, reactor, threads
from time import sleep, time
from random import randint
from metagit import MetaGit,MetaGitException

class Approvers(object):
  def __init__(self, users_override=[]):
    self.approvers = []
    self.users_override = users_override
  def __call__(self):
    return self.approvers
  @staticmethod
  def from_str(s, users_override=[]):
    match = re.findall("([0-9]+) of ([^;]+)?", s)
    a = Approvers(users_override=users_override)
    for m in match:
      a.push(int(m[0]), list(map(Approvers.ghstrip, str(m[1]).split(","))))
    return a
  def approve(self, user):
    if user in self.users_override:
      self.approvers = True
      return True
    if self.approvers == True:
      return True
    ok = False
    for a in self.approvers:
      try:
        a["u"].remove(user)
        a["n"] = a["n"]-1
        ok = True
      except (KeyError,ValueError):
        pass
    if ok:
      self.approvers = [ a for a in self.approvers if a["n"] >= 1 ]
      if not self.approvers:
        self.approvers = True
    return ok
  def push(self, num_approvers, approvers):
    if not self.approvers and isinstance(approvers, bool):
      self.approvers = approvers
    elif isinstance(self.approvers, bool) and not isinstance(approvers, bool):
      self.approvers = [{ "n":num_approvers, "u":approvers }]
    elif not isinstance(approvers, bool):
      found = False
      for x in self.approvers:
        if x["u"] == approvers:
          x["n"] = max(x["n"], num_approvers)  # be restrictive when updating existing group
          found = True
          break
      if not found:
        self.approvers.append({ "n":num_approvers, "u":approvers })
  def flat(self):
    f = set()
    if self.approvers == True:
      return f
    for x in self.approvers:
      f.update(x["u"])
    return f
  def ghtagmap(self, u):
    if u in self.usermap:
      return "@%s (%s)" % (u, self.usermap[u])
    return "@"+u
  @staticmethod
  def ghstrip(u):
    try:
      u = u[0:u.index("(")]
    except ValueError:
      pass
    return u.strip("@ ")
  def __str__(self):
    if self.approvers == True:
      return "approved"
    approval_str = [ "%d of %s" % (x["n"], ", ".join(map(self.ghtagmap, x["u"]))) for x in self.approvers ]
    return "; ".join(approval_str)

class State(object):

  def __init__(self, name, sha=None, approvers=Approvers(), opener=None,
               dryRun=False, haveApproved=[], haveApproved_p2=[]):
    self.name                = name
    self.sha                 = sha
    self.approvers           = approvers
    self.opener              = opener
    self.dryRun              = dryRun
    self.haveApproved        = haveApproved
    self.haveApproved_p2     = haveApproved_p2
    self.approvers_unchanged = Approvers.from_str(str(approvers), approvers.users_override)
    if name == "STATE_INITIAL":
      self.action = self.action_check_permissions
    elif name == "STATE_APPROVAL_REQUIRED":
      self.action = self.action_approval_required
    elif name == "STATE_APPROVAL_PENDING":
      self.action = self.action_approval_pending
    elif name == "STATE_TESTS_ONLY":
      self.action = self.action_tests_only
    elif name == "STATE_TESTS_AUTOMERGE":
      self.action = self.action_tests_automerge
    elif name == "STATE_MERGE_APPROVAL_PENDING":
      self.action = self.action_approval2_pending
    else:
      raise RuntimeError("unknown state %s" % self.name)

  def __str__(self):
    return "%s: sha: %s, approvers: %s, opener: %s, have approved: %s, have approved (2): %s" % \
           (self.name, self.sha, self.approvers, self.opener, self.haveApproved, self.haveApproved_p2)

  def action_check_permissions(self, git, pr, perms, tests):
    pull = git.get_pull(pr, cached=True)
    if pull.changed_files > 50:
      # Too many changed files. It's not worth to check every single one of them. This would also
      # exhaust the API calls. Let's ask for the approval of the masters only.
      info("this pull request has %d (> 50) changed files: requesting approval from the admins only" % \
           pull.changed_files)
      self.approvers.push(1, self.approvers.users_override)
    else:
      for fn in pull.get_files():
        debug("determining permissions for file %s" % fn)
        for rule in perms:
          num_approve,approve = rule(fn, pull.who)  # approve can be bool or set (not list)
          if approve:
            debug("file %s matched by rule %s: %s" % (fn, rule.path_regexp, approve))
            self.approvers.push(num_approve, approve)
            break
        assert approve, "this should not happen: for file %s no rule matches" % fn
    debug("computed list of approvers: %s (override: %s)" % (self.approvers, self.approvers.users_override))
    self.approvers_unchanged = Approvers.from_str(str(self.approvers), users_override=self.approvers.users_override)
    self.action_approval_required(git, pr, perms, tests)

  def action_approval_required(self, git, pr, perms, tests):
    pull = git.get_pull(pr, cached=True)
    self.action_approval_pending(git, pr, perms, tests)
    if self.approvers() != True:
      info("%s: approval required by: %s" % (self.sha, self.approvers))
      self.request_approval(git, pr)

  def request_approval(self, git, pr):
    git.set_status(pr, "review", "pending", "pending approval")
    pull = git.get_pull(pr, cached=True)
    git.add_comment(pr, "%s: approval required: %s\n\n" \
                        "_Comment with `+1` to approve and allow automatic merging," \
                        "or with `+test` to run tests only. **Please comment on the pull request: " \
                        "[click here](https://github.com/%s/pull/%d) and comment at the bottom of " \
                        "the page.**_" % \
                        (self.sha, self.approvers, pull.repo, pull.num))

  def action_approval_pending(self, git, pr, perms, tests):
    approveTestsOnly = False
    hasChanged = False
    for u in self.haveApproved:
      if self.approvers.approve(u["u"]):
        hasChanged = True
        if u["what"] == "test":
          approveTestsOnly = True
    if self.approvers() == True:
      git.set_status(pr, "review", "success", \
                         "tests approved" if approveTestsOnly else "merge approved")
      for t in tests:
        git.set_status(pr, t, "pending", "test required")
    if self.approvers() == True and approveTestsOnly:
      info("%s: only testing approved, no auto merge on test success" % self.sha)
      git.add_comment(pr, "%s: testing approved: " \
                          "will not be automatically merged; starting testing. " \
                          "If testing succeeds, merging will require further approval from %s" % \
                          (self.sha, str(self.approvers_unchanged)))
    elif self.approvers() == True:
      info("%s: changes approved, auto merge on test success" % self.sha)
      git.add_comment(pr, "%s: approved: will be automatically merged on successful tests" % self.sha)
    else:
      review_status = git.get_status(pr, "review")[0]
      if hasChanged or (review_status is not None and review_status != "pending"):
        info("%s: list of approvers has changed to %s, notifying" % (self.sha, self.approvers))
        self.request_approval(git, pr)
      else:
        info("%s: list of approvers unchanged, nothing to say" % self.sha)

  def action_approval2_pending(self, git, pr, perms, tests):
    hasChanged = False
    for u in self.haveApproved_p2:
      if self.approvers.approve(u["u"]):
        hasChanged = True
    if self.approvers() == True:
      info("%s: merge approved, merging now" % self.sha)
      git.merge(pr)
    else:
      review_status = git.get_status(pr, "review")[0]
      if review_status != "success":
        git.set_status(pr, "review", "success", "tests approved")  # conflicts gone: restore
      if hasChanged or review_status != "success":
        info("%s: list of merge approvers has changed to %s, notifying" % (self.sha, self.approvers))
        self.request_approval(git, pr)
      else:
        info("%s: list of merge approvers unchanged, nothing to say" % self.sha)

  def action_tests_only(self, git, pr, perms, tests):
    ok = False
    if git.get_status(pr, "review")[0] != "success":
      git.set_status(pr, "review", "success", "tests approved")  # conflicts gone: restore
    for x in tests:
      s = git.get_status(pr, x)[0]
      debug("%s: required test %s is %s" % (self.sha, x, s))
      ok = (s == "success")
      if not ok:
        break
    if ok:
      pull = git.get_pull(pr, cached=True)
      info("%s: all tests passed, requesting approval from %s" % (self.sha, self.approvers))
      git.add_comment(pr, "%s: tests OK, approval required for merging: %s\n\n" \
                          "_Comment with `+1` to merge. **Please comment on the pull request: " \
                          "[click here](https://github.com/%s/pull/%d) and comment at the bottom " \
                          "of the page.**_" % \
                          (self.sha, str(self.approvers), pull.repo, pull.num))
    info("%s: tests are currently in progress, will not auto merge on success" % self.sha)

  def action_tests_automerge(self, git, pr, perms, tests):
    ok = False
    if git.get_status(pr, "review")[0] != "success":
      git.set_status(pr, "review", "success", "merge approved")  # conflicts gone
    for x in tests:
      s = git.get_status(pr, x)[0]
      debug("%s: required test %s is %s" % (self.sha, x, s))
      ok = (s == "success")
      if not ok:
        break
    if ok:
      git.merge(pr)
    else:
      info("%s: tests are currently in progress, will auto merge on success" % self.sha)

class Transition(object):

  def __init__(self, regexp, final_state, from_states):
    self.regexp      = regexp
    self.final_state = final_state
    self.from_states = from_states

  def __str__(self):
    return "PR Transition: user: %s, regexp: %s, new state: %s, from: %s" % (self.user, self.regexp, self.final_state, self.from_states)

  def evolve(self, state, opener, first_line, extra_allowed_openers):
    allowed_openers = state.approvers.flat()
    allowed_openers.update(extra_allowed_openers)
    debug("evolve: %s ==> %s, allowed: %s, regexp: %s, from: %s" % \
          (state.name, self.final_state, allowed_openers, self.regexp, self.from_states))
    if state.name not in self.from_states:
      debug("evolve: from state %s unallowed" % state.name)
      return state

    match = re.search(self.regexp, first_line)
    named_matches = match.groupdict() if match else None
    if not named_matches:
      debug("evolve: comment does not match")
      return state
    if named_matches.get("sha", state.sha) != state.sha:
      debug("evolve: comment does not pertain to current sha")
      return state

    if "approval" in named_matches:
      # Whoever wrote the comment is considered. Authz is checked later on
      new_state = State(name=state.name,
                        sha=state.sha,
                        approvers=state.approvers,
                        opener=state.opener,
                        dryRun=state.dryRun,
                        haveApproved=state.haveApproved,
                        haveApproved_p2=state.haveApproved_p2)
      ha = new_state.haveApproved_p2 if state.name == "STATE_MERGE_APPROVAL_PENDING" else new_state.haveApproved
      ha.append({"u":opener,"what":"test" if named_matches["approval"] == "test" else "merge"})
      debug("evolve: list of approvers updated: %s" % new_state.haveApproved)
    elif not opener in allowed_openers:
      debug("evolve: opener %s unallowed to move to state %s" % (opener, self.final_state))
      return state
    elif "approvers" in named_matches:
      app = Approvers.from_str(named_matches["approvers"], users_override=extra_allowed_openers)
      if not app:
        debug("evolve: cannot match approvers")
        return state
      new_state = State(name=self.final_state,
                        sha=state.sha,
                        approvers=app,
                        opener=opener,
                        dryRun=state.dryRun,
                        haveApproved=state.haveApproved,
                        haveApproved_p2=state.haveApproved_p2)
    else:
      debug("evolve: approved")
      new_state = State(name=self.final_state,
                        sha=state.sha,
                        approvers=state.approvers,
                        opener=opener,
                        dryRun=state.dryRun,
                        haveApproved=state.haveApproved,
                        haveApproved_p2=state.haveApproved_p2)

    return new_state

TRANSITIONS = [
  Transition("^\+(?P<approval>1|test)",
             None,
             ["STATE_INITIAL", "STATE_APPROVAL_REQUIRED", "STATE_APPROVAL_PENDING", "STATE_MERGE_APPROVAL_PENDING"]),
  Transition("^(?P<sha>[a-fA-F0-9]+): approval required[^:]*: (?P<approvers>.*)",
             "STATE_APPROVAL_PENDING",
             ["STATE_INITIAL", "STATE_APPROVAL_PENDING"]),
  Transition("^(?P<sha>[a-fA-F0-9]+): testing approved.*further approval from (?P<approvers>.*)",
             "STATE_TESTS_ONLY",
             ["STATE_INITIAL", "STATE_APPROVAL_PENDING"]),
  Transition("^(?P<sha>[a-fA-F0-9]+): approved",
             "STATE_TESTS_AUTOMERGE",
             ["STATE_INITIAL", "STATE_APPROVAL_PENDING"]),
  Transition("^(?P<sha>[a-fA-F0-9]+): tests OK, approval required[^:]*: (?P<approvers>.*)",
             "STATE_MERGE_APPROVAL_PENDING",
             ["STATE_TESTS_ONLY"])
]

class Perms(object):

  def __init__(self, path_regexp, authorized, approve, num_approve):
    self.path_regexp = path_regexp
    self.authorized = authorized
    self.approve = approve
    self.num_approve = num_approve

  def path_match(self, path):
    try:
      return True if re.search(self.path_regexp, path) else False
    except re.error as e:
      warning("path regular expression %s is not valid: %s" % (self.path_regexp, e))
      return False

  def __call__(self, path, current_user):
    if self.path_match(path):
      if current_user in self.authorized:
        return 0,True
      else:
        return self.num_approve,set(self.approve)
    return 0,False

class PrRPC(object):
  app = Klein()
  items = set()

  def __init__(self, host, port, bot_user, admins, processQueueEvery, processAllEvery,
               processStuckThreshold, dummyGit, dryRun):
    self.bot_user = bot_user
    self.admins = admins
    self.dryRun = dryRun
    self.must_exit = False
    self.processStartTime = 0
    self.processStuckThreshold = processStuckThreshold
    self.git = MetaGit.init(backend="Dummy" if dummyGit else "GitHub",
                            bot_user=bot_user,
                            store="dummy",
                            token=open(expanduser("~/.github-token")).read().strip(),
                            rw=not dryRun)

    def set_must_exit():
      self.must_exit = True
    reactor.addSystemEventTrigger("before", "shutdown", set_must_exit)

    def schedule_process_pull_requests():
      items_to_process = self.items.copy()
      self.items = set()  # empty queue
      self.processStartTime = time()
      def resetStartTime(x):
        self.processStartTime = 0
      d = threads.deferToThread(self.process_pull_requests, items_to_process,
                                                            self.bot_user,
                                                            self.admins,
                                                            self.dryRun)
      d.addCallback(lambda unprocessed: self.items.update(unprocessed))  # repush (requeue) unprocessed
      d.addErrback(lambda x: error("Uncaught exception during pull request test: %s" % str(x)))
      d.addBoth(lambda x: reactor.callLater(processQueueEvery, schedule_process_pull_requests))
      d.addBoth(resetStartTime)
      return d

    if processAllEvery <= 0:
      warning("Pull requests will be processed only upon callbacks")
    else:
      reactor.callLater(1, LoopingCall(self.add_all_open_prs).start, processAllEvery)

    reactor.callLater(10, schedule_process_pull_requests)
    self.app.run(host, port)

  def j(self, req, obj):
    req.setHeader("Content-Type", "application/json")
    return json.dumps(obj, default=lambda o: o.__dict__)

  def add_all_open_prs(self):
    perms,_,_ = load_perms("perms.yml", "groups.yml", "mapusers.yml", admins=args.admins.split(","))
    for repo in perms:
      try:
        newpr = self.git.get_pulls(repo)
        self.items.update(newpr)
        for n in newpr:
          debug("Process all: appending %s" % n)
      except MetaGitException as e:
        warning("Cannot get pulls for %s: %s" % (repo, e))

  # Processes a list of pull requests. Takes as input an iterable of pull requests
  # in the form Group/Repo#PrNum and returns a set of processed pull requests.
  def process_pull_requests(self, prs, bot_user, admins, dryRun):
    info("Processing all scheduled pull requests: %d to go" % len(prs))
    unprocessed = set(prs)  # keep track of what could not be processed

    # Load permissions as first thing
    perms,tests,usermap = load_perms("perms.yml", "groups.yml", "mapusers.yml", admins=admins)
    #debug("permissions:\n"+json.dumps(perms, indent=2, default=lambda o: o.__dict__))
    #debug("tests:\n"+json.dumps(tests, indent=2))
    #debug("GitHub to full names mapping:\n"+json.dumps(usermap, indent=2))
    setattr(Approvers, "usermap", usermap)

    for pr in prs:
      if self.must_exit:
        info("Interrupting loop: must exit")
        break
      repo,prnum = pr.split("#", 1)
      prnum = int(prnum)
      debug("Queued PR: %s#%d" % (repo,prnum))
      if not repo in perms:
        debug("Skipping %s: not a configured repository" % pr)
        unprocessed.remove(pr)
        continue

      ok = False
      try:
        ok = self.pull_state_machine(pr,
                                     perms.get(repo, []),
                                     tests.get(repo, []),
                                     bot_user,
                                     admins,
                                     dryRun)
      except MetaGitException as e:
        error("Cannot process pull request %s#%d, removing from list: %s" % (repo, prnum, e))
        ok = True
      except Exception as e:
        error("Cannot process pull request %s#%d, retrying, strange error: %s" % (repo, prnum, e))

      # PR can be removed from list
      if ok:
        unprocessed.remove(pr)
    return unprocessed  # empty set in case of full success

  def pull_state_machine(self, pr, perms, tests, bot_user, admins, dryRun):
    pull = self.git.get_pull(pr)
    info("")
    info("~~~ processing %s: %s (changed files: %d) ~~~" % (pr, pull.title, pull.changed_files))

    if pull.closed_at:
      info("%s: skipping: closed" % pr)
      return True

    if not pull.changed_files:
      if self.git.get_status(pr, "review") != ("error", "empty pull request"):
        self.git.add_comment(pr, ("@%s: your pull request changes no files (%s)." + \
                                  "You may want to fix it or close it.") % \
                                  (pull.who, pull.sha))
        self.git.set_status(pr, "review", "error", "empty pull request")
      info("%s: skipping: empty!" % pr)
      return True

    if not pull.mergeable:
      if pull.mergeable_state == "dirty":
        # It really cannot be merged. Notify user
        if self.git.get_status(pr, "review") != ("error", "conflicts"):
          self.git.add_comment(pr, ("@%s: there are conflicts in your changes (%s) you need to fix.\n\n" + \
                                    "_You can have a look at the "                                       + \
                                    "[documentation](http://alisw.github.io/git-advanced/) or you can "  + \
                                    "press the **Resolve conflicts** button and try to fix them from "   + \
                                    "the web interface._") % (pull.who, pull.sha))
          self.git.set_status(pr, "review", "error", "conflicts")
        info("%s: skipping: cannot merge" % pr)
        return True
      else:  # mergeable_state is "unknown"
        info("%s: skipping checking mergeability (state is \"%s\")" % (pr, pull.mergeable_state))
        return False  # we should come back to it

    state = State(name="STATE_INITIAL",
                  sha=pull.sha,
                  dryRun=args.dryRun,
                  approvers=Approvers(users_override=admins),
                  haveApproved=[],
                  haveApproved_p2=[])

    for comment in self.git.get_comments(pr):
      if (comment.when-pull.when).total_seconds() < 0:
        info("* %s @ %s UTC: %s ==> skipping" % (comment.who, comment.when, comment.short))
        continue
      info("* %s @ %s UTC: %s" % (comment.who, comment.when, comment.short))
      for transition in TRANSITIONS:
        new_state = transition.evolve(state, comment.who, comment.short, [bot_user]+admins)
        if not new_state is state:
          # A transition occurred
          info("  ==> %s" % new_state)
          state = new_state
          break

    info("Final state is %s: executing action" % state)
    state.action(self.git, pr, perms, tests)
    return True

  @app.route("/", methods=["POST"])
  def github_callback(self, req):
    data = json.loads(req.content.read())
    repo = data.get("repository", {}).get("full_name", None)  # always there
    prid = None
    if "pull_request" in data and data.get("action") in [ "opened", "synchronize" ]:
      # EVENT: pull request just opened
      prid = data.get("number", None)
      etype = "pull request opened"
    elif "state" in data and "context" in data and "sha" in data:
      # EVENT: state changed, let's hope we have the hash in cache
      fullpr = self.git.get_pull_from_sha(data["sha"])
      if fullpr:
        etype = "state changed"
        prid = fullpr.num
      else:
        warning("State changed event was unhandled: could not find %s in cache" % data["sha"])
    elif "issue" in data and data.get("action") == "created" \
      and isinstance(data["issue"].get("pull_request", None), dict) \
      and data["issue"].get("closed_at", True) is None \
      and data.get("sender", {}).get("login", "alibuild") != "alibuild":
      # EVENT: comment added on an open pull request (and not by a bot)
      prid = data["issue"].get("number", None)
      etype = "pull request commented"
    if repo and prid:
      prid = int(prid)
      prfull = "%s#%d" % (repo, prid)
      info("Received relevant event (%s) for %s" % (etype, prfull))
      self.items.add(prfull)
    else:
      debug("Received unhandled event from GitHub:\n%s" % json.dumps(data, indent=2))
    return "roger"

  @app.route("/list")
  def get_list(self, req):
    return self.j(req, {"queued": list(self.items)})

  @app.route("/perms")
  def check_loaded_perms(self, req):
    perms,tests,usermap = load_perms("perms.yml", "groups.yml", "mapusers.yml", admins=self.admins)
    out = { "perms": perms, "tests": tests, "usermap": usermap }
    return self.j(req, out)

  @app.route("/process/all")
  def process_all(self, req):
    self.add_all_open_prs()
    return self.j(req, {"queued": list(self.items)})

  @app.route("/process/<group>/<repo>/<prid>")
  def process(self, req, group, repo, prid):
    pr = "%s/%s#%d" % (group, repo, int(prid))
    self.items.add(pr)
    return self.j(req, {"added_to_queue": pr})

  @app.route("/health")
  def health(self, req):
    runningSince = time()-self.processStartTime if self.processStartTime else 0
    if runningSince > self.processStuckThreshold:
      req.setResponseCode(500)
      status = "stuck"
    else:
      status = "ok"
    return self.j(req, {"status"           : status,
                        "running_since"    : runningSince,
                        "stuck_threshold_s": self.processStuckThreshold })

# Parse file
def load_perms(f_perms, f_groups, f_mapusers, admins):
  perms = {}
  groups = {}
  mapusers = {}
  tests = {}
  realnames = {}

  # Load user mapping (CERN -> GitHub)
  try:
    mapusers = yaml.safe_load(open(f_mapusers))
    for k in mapusers:
      if not " " in mapusers[k]:
        mapusers[k] = mapusers[k] + " " + mapusers[k]
      un,real = mapusers[k].split(" ", 1)
      realnames[un] = real  # gh -> full
      mapusers[k] = un      # cern -> gh
  except (IOError,yaml.YAMLError) as e:
    error("cannot load user mapping from %s: %s" % (f_mapusers, e))

  # Load external groups
  try:
    groups = yaml.safe_load(open(f_groups))
    for k in groups:
      groups[k] = groups[k].split()
  except (IOError,yaml.YAMLError) as e:
    error("cannot load external groups from %s: %s" % (f_groups, e))

  # Load permissions
  try:
    c = yaml.safe_load(open(f_perms))
  except (IOError,yaml.YAMLError) as e:
    error("cannot load permissions from %s: %s" % (f_perms, e))
    c = {}
  for g in c.get("groups", {}):
    # Get internal groups (they override external groups with the same name)
    groups[g] = list(set(c["groups"][g].split()))
  for repo in c:
    if not "/" in repo: continue
    try:
      tests[repo] = c[repo].get("tests", [])
    except (KeyError,TypeError) as e:
      warning("config %s: wrong syntax for tests in repo %s" % (f_perms, repo))
      tests[repo] = []
    try:
      rules = c[repo].get("rules", [])
    except (KeyError,TypeError) as e:
      warning("config %s: wrong syntax for rules in repo %s" % (f_perms, repo))
      rules = []
    try:
      repo_admins = c[repo]["admins"].split(",")  # admins are mentioned as approvers in every PR
    except (KeyError,TypeError) as e:
      repo_admins = []
    perms[repo] = []
    for path_rule in rules:
      if not isinstance(path_rule, dict):
        warning("config %s: skipping unknown token %s" % (f_perms,path_rule))
        continue
      for path_regexp in path_rule:
        auth = path_rule[path_regexp].split()
        approve = []
        num_approve = 1
        for a in auth:
          if a.startswith("approve="): approve = a[8:].split(",")
          elif a.startswith("num_approve="):
            try:
              num_approve = int(a[12:])
              if num_approve < 1: raise ValueError
            except ValueError as e:
              warning("config %s: invalid %s for repo %s path %s: fallback to 1" % \
                      (cf, a, repo, path_regexp))
              num_approve = 1
        approve += repo_admins  # add admins as approvers to every PR

        auth = [ x for x in auth if not "=" in x ]
        # Append rule to perms
        perms[repo].append(Perms(path_regexp=path_regexp,
                                 authorized=auth,
                                 approve=approve,
                                 num_approve=num_approve))

  # Resolve groups (unknown discarded)
  for repo in perms:
    for path_rule in perms[repo]:
      for k in ["authorized", "approve"]:
        users = set()
        for u in getattr(path_rule, k):
          if u[0] == "@": users.update(groups.get(u[1:], []))
          else: users.add(u)
        # Map users (unknown discarded)
        setattr(path_rule, k, list(set([ mapusers[u] for u in users if u in mapusers ])))
      if not path_rule.approve:
        #warning("empty list of approvers for %s on %s: defaulting to admins" % \
        #        (path_rule.path_regexp, repo))
        path_rule.approve = admins
      path_rule.num_approve = min(path_rule.num_approve, len(path_rule.approve))

  # Append catch-all default rule to all repos: we *always* match something
  for repo in perms:
    perms[repo].append(Perms(path_regexp="^.*$",
                             authorized=[],  # TODO use authorized=admins in production
                             approve=admins,
                             num_approve=1))

  return perms,tests,realnames

if __name__ == "__main__":

  parser = ArgumentParser()
  parser.add_argument("--dry-run", dest="dryRun",
                      action="store_true", default=False,
                      help="Do not modify Github")
  parser.add_argument("--debug", dest="debug",
                      action="store_true", default=False,
                      help="Be verbose in debug output")
  parser.add_argument("--more-debug", dest="more_debug",
                      action="store_true", default=False,
                      help="Include GitHub API debug output")
  parser.add_argument("--bot-user", dest="bot_user",
                      help="GitHub bot username (mandatory)")
  parser.add_argument("--admins", dest="admins",
                      help="Comma-separated list of GitHub usernames of admins (mandatory)")
  parser.add_argument("--process-queue-every", dest="processQueueEvery", default=30, type=int,
                      help="Accumulate pull requests and process them every that many seconds")
  parser.add_argument("--process-all-every", dest="processAllEvery", default=600, type=int,
                      help="Process all pull requests every that many seconds (0: callbacks only)")
  parser.add_argument("--process-stuck-threshold", dest="processStuckThreshold", default=300, type=int,
                      help="Report as unhealthy if too long in the process loop (defaults 300 s)")
  parser.add_argument("--dummy-git", dest="dummyGit",
                      action="store_true", default=False,
                      help="Use the dummy Git backend for testing")
  args = parser.parse_args()
  if args.more_debug:
    args.debug = True
  if not args.bot_user:
    parser.error("Please specify the bot's user name on GitHub (--bot-user)")
  if not args.admins:
    parser.error("Please specify the GitHub usernames of admins (--admins)")
  if args.processQueueEvery < 5:
    parser.error("--process-queue-every must be at least 5 seconds")
  if args.processAllEvery > 0 and args.processAllEvery < 10:
    parser.error("--process-all-every must be either 0 (disable) or at least 10 seconds")

  logger = logging.getLogger()
  loggerHandler = logging.StreamHandler()
  loggerHandler.setFormatter(logging.Formatter('%(levelname)s:%(name)s: %(message)s'))
  if args.debug: logger.setLevel(logging.DEBUG)
  else: logger.setLevel(logging.INFO)
  if not args.more_debug:
    logging.getLogger("github").setLevel(logging.WARNING)
  logger.addHandler(loggerHandler)

  prrpc = PrRPC(host="0.0.0.0",
                port=8000,
                bot_user=args.bot_user,
                admins=args.admins.split(","),
                processQueueEvery=args.processQueueEvery,
                processAllEvery=args.processAllEvery,
                processStuckThreshold=args.processStuckThreshold,
                dummyGit=args.dummyGit,
                dryRun=args.dryRun)
