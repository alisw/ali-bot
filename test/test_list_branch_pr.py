"""Pin the behaviour of list-branch-pr that the production builders depend on.

Every CI builder runs this script to decide what to build, so a change in what
it prints, or in what order, changes what the whole CI does. The tests below are
mostly *characterisation* tests: they assert today's behaviour, not an ideal, so
that altering it has to be deliberate rather than incidental.

The script is loaded by path (it has no .py extension and is not a module), with
its two network-facing dependencies stubbed: the GraphQL client, which lists
pull requests, and GithubCachedClient, which is the REST client used only for
writing statuses. Nothing here touches the network.

The invariants worth knowing about, because breaking them breaks production
rather than a test:

  * the output is exactly five tab-separated fields. continuous-builder.sh
    reads six names from `cat -n | while read`, and `read` folds any extra field
    into the last variable -- so a sixth column silently corrupts WAITING_SINCE
    on every builder.
  * without --all-groups the script emits either every untested PR, or exactly
    one already-tested PR to rebuild. Never both, never several rebuilds.
  * hash sharding partitions the PRs: over all worker indices every PR appears
    exactly once. Two workers building the same PR, or a PR no worker builds,
    are both silent failures.
  * --no-status makes the run read-only. The collector relies on it to survey
    the queue without annotating anyone's pull requests.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

CHECK_NAME = "build/O2/alidist-test"
PR_REPO = "alisw/alidist"


def load_script():
    """Load list-branch-pr as a module, despite having no .py extension."""
    loader = importlib.machinery.SourceFileLoader(
        "list_branch_pr", os.path.join(REPO, "list-branch-pr"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class CountingRestClient:
    """Stands in for GithubCachedClient, counting calls rather than making them.

    Only setGithubStatus uses it, so a non-zero count means the run wrote (or
    tried to write) to GitHub.
    """

    def __init__(self):
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, *args, **kwargs):
        self.calls += 1
        return []          # no existing statuses

    def post(self, *args, **kwargs):
        self.calls += 1


def make_pr(number, committed_day, state=None, built_day=None, labels=(),
            draft=False, title="a pull request", association="MEMBER",
            approved=True, reviewed=True, broken=0):
    """One node shaped like the GraphQL query's pullRequests.nodes entries.

    state=None means this check has never run on the commit (untested);
    "SUCCESS"/"FAILURE" mean it has, and built_day says when.

    broken=N adds N unrelated checks, all red, which is what the queue
    ordering reads to decide how much this PR is worth building.
    """
    # A passing "review" context is one of the two ways a PR becomes
    # buildable: process_single_pr requires `reviewed or is_trusted`, so a PR
    # with this context is built even from an untrusted author.
    contexts = [{"context": "review",
                 "state": "SUCCESS" if reviewed else "PENDING",
                 "createdAt": "2024-01-01T00:00:00Z"}]
    contexts.extend({"context": "build/other-%d" % i,
                     "state": "ERROR" if i % 2 else "FAILURE",
                     "createdAt": "2024-01-01T00:00:00Z"}
                    for i in range(broken))
    if state is not None:
        contexts.append({"context": CHECK_NAME, "state": state,
                         "createdAt": "2024-02-%sT00:00:00Z" % (built_day or "01")})
    return {
        "number": number,
        "title": title,
        "isDraft": draft,
        "createdAt": "2023-01-01T00:00:00Z",
        "authorAssociation": association,
        "author": {"login": "someone"},
        "reviews": {"isApproved": approved},
        "labels": {"nodes": [{"name": name} for name in labels]},
        "commits": {"nodes": [{"commit": {
            "oid": "sha%d" % number,
            "committedDate": "2024-01-%sT00:00:00Z" % committed_day,
            "status": {"contexts": contexts},
        }}]},
    }


class ListBranchPRTestCase(unittest.TestCase):
    def setUp(self):
        self.script = load_script()
        # A definitions tree of our own, so the test does not break when a real
        # check is renamed or retired.
        self.definitions = tempfile.mkdtemp()
        check_dir = os.path.join(self.definitions, "role", "container")
        os.makedirs(check_dir)
        self.check_env = os.path.join(check_dir, "acheck.env")
        self.write_check_env()

    def write_check_env(self, extra=""):
        """(Re)write the check definition, optionally with extra settings."""
        with open(self.check_env, "w") as envf:
            envf.write("CHECK_NAME=%s\nPR_REPO=%s\nPR_BRANCH=master\n%s"
                       % (CHECK_NAME, PR_REPO, extra))

    def run_script(self, pulls, *, all_groups=False, no_status=True,
                   worker_index=0, worker_pool_size=1):
        """Run main() over `pulls`, returning (rows, rest_call_count).

        rows is a list of the tab-separated fields of each output line.
        """
        rest = CountingRestClient()
        self.script.query_repo_info = \
            lambda *a, **k: {"pullRequests": {"nodes": pulls}}
        self.script.github_token = lambda: "not-a-real-token"
        # Record how the GraphQL transport was built: which URL, and whether
        # the credential went as an auth= tuple (HTTP Basic) or a header.
        self.transport_kwargs = {}
        self.script.RequestsHTTPTransport = \
            lambda **kwargs: self.transport_kwargs.update(kwargs)
        self.script.Client = lambda **kwargs: contextlib.nullcontext(None)
        self.script.GithubCachedClient = lambda *a, **k: rest

        from argparse import Namespace
        args = Namespace(definitions_dir=self.definitions, mesos_role="role",
                         container_name="container", config_suffix="",
                         worker_index=worker_index,
                         worker_pool_size=worker_pool_size,
                         show_base_branch=False, all_groups=all_groups,
                         no_status=no_status)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(io.StringIO()):
            self.script.main(args)
        rows = [line.split("\t") for line in stdout.getvalue().splitlines()]
        return rows, rest.calls

    # ---- the output contract ------------------------------------------------

    def test_output_is_always_five_fields(self):
        """A sixth column would corrupt WAITING_SINCE on every builder."""
        pulls = [make_pr(1, "01"), make_pr(2, "02", "FAILURE", "05"),
                 make_pr(3, "03", "SUCCESS", "06")]
        for all_groups in (False, True):
            rows, _ = self.run_script(pulls, all_groups=all_groups)
            self.assertTrue(rows, "expected some output")
            for row in rows:
                self.assertEqual(len(row), 5, "row %r" % (row,))

    def test_untested_are_all_emitted_oldest_first(self):
        pulls = [make_pr(3, "03"), make_pr(1, "01"), make_pr(2, "02")]
        rows, _ = self.run_script(pulls)
        self.assertEqual([row[0] for row in rows], ["untested"] * 3)
        self.assertEqual([row[1] for row in rows], ["1", "2", "3"])

    def test_untested_carry_an_epoch_waiting_since(self):
        rows, _ = self.run_script([make_pr(1, "01")])
        self.assertEqual(rows[0][0], "untested")
        self.assertTrue(rows[0][4].isdigit(), "expected an epoch, got %r" % rows[0][4])

    def test_exactly_one_rebuild_when_nothing_is_untested(self):
        """The default path must never drain the rebuild backlog."""
        pulls = [make_pr(n, "0%d" % n, "FAILURE", "0%d" % n) for n in (1, 2, 3)] + \
                [make_pr(n, "0%d" % n, "SUCCESS", "0%d" % n) for n in (4, 5, 6)]
        for _ in range(20):        # the choice is random; the count is not
            rows, _ = self.run_script(pulls)
            self.assertEqual(len(rows), 1, "expected one rebuild, got %r" % (rows,))
            self.assertIn(rows[0][0], ("failed", "succeeded"))

    def test_rebuilds_have_no_waiting_since_by_default(self):
        """For a PR built before, the commit date is not a waiting time."""
        rows, _ = self.run_script([make_pr(1, "01", "FAILURE", "05")])
        self.assertEqual(rows[0][4], "")

    def test_untested_wins_over_rebuilds(self):
        pulls = [make_pr(1, "01"), make_pr(2, "02", "FAILURE", "05")]
        rows, _ = self.run_script(pulls)
        self.assertEqual([row[0] for row in rows], ["untested"])

    # ---- skipping -----------------------------------------------------------

    def test_drafts_and_wip_are_skipped(self):
        pulls = [make_pr(1, "01", draft=True),
                 make_pr(2, "02", title="[WIP] not ready"),
                 make_pr(3, "03")]
        rows, _ = self.run_script(pulls)
        self.assertEqual([row[1] for row in rows], ["3"])

    def test_untrusted_unapproved_prs_are_skipped(self):
        pulls = [make_pr(1, "01", association="NONE", approved=False,
                         reviewed=False),
                 make_pr(2, "02")]
        rows, _ = self.run_script(pulls)
        self.assertEqual([row[1] for row in rows], ["2"])

    # ---- sharding -----------------------------------------------------------

    def test_sharding_partitions_the_prs(self):
        """Every PR is built by exactly one worker: no duplicates, none lost.

        And the work is spread. Asserting only that the shards partition the
        set is too weak: a hash that sent every PR to worker 0 would satisfy it
        while leaving the rest of the pool idle -- verified by mutation, which
        is how this assertion came to be here.
        """
        pulls = [make_pr(n, "01") for n in range(1, 21)]
        pool = 4
        per_worker = {}
        for index in range(pool):
            rows, _ = self.run_script(pulls, worker_index=index,
                                      worker_pool_size=pool)
            per_worker[index] = [row[1] for row in rows]

        seen = [pr for prs in per_worker.values() for pr in prs]
        self.assertEqual(sorted(seen, key=int),
                         [str(n) for n in range(1, 21)])
        self.assertEqual(len(seen), len(set(seen)), "a PR was claimed twice")
        for index, prs in per_worker.items():
            self.assertTrue(prs, "worker %d got no work at all" % index)

    def test_pool_size_one_sees_everything(self):
        """How the claim-based workers switch sharding off without a code change."""
        pulls = [make_pr(n, "01") for n in range(1, 11)]
        rows, _ = self.run_script(pulls, worker_index=0, worker_pool_size=1)
        self.assertEqual(len(rows), 10)

    # ---- read-only listing --------------------------------------------------

    def test_no_status_makes_no_rest_calls(self):
        """What lets the queue collector survey without annotating PRs."""
        pulls = [make_pr(1, "01", association="NONE", approved=False,
                         reviewed=False)]
        _, calls = self.run_script(pulls, no_status=True)
        self.assertEqual(calls, 0)

    def test_without_no_status_an_untrusted_pr_is_annotated(self):
        pulls = [make_pr(1, "01", association="NONE", approved=False,
                         reviewed=False)]
        _, calls = self.run_script(pulls, no_status=False)
        self.assertGreater(calls, 0)

    # ---- --all-groups: the order a claiming worker walks --------------------

    def test_all_groups_emits_every_group(self):
        pulls = [make_pr(1, "01"), make_pr(2, "02", "FAILURE", "05"),
                 make_pr(3, "03", "SUCCESS", "06")]
        rows, _ = self.run_script(pulls, all_groups=True)
        self.assertEqual(sorted(row[0] for row in rows),
                         ["failed", "succeeded", "untested"])

    def test_all_groups_puts_untested_first(self):
        pulls = [make_pr(2, "02", "FAILURE", "05"), make_pr(1, "01")]
        rows, _ = self.run_script(pulls, all_groups=True)
        self.assertEqual(rows[0][0], "untested")

    def test_all_groups_is_deterministic(self):
        pulls = [make_pr(n, "0%d" % n, "FAILURE", "0%d" % n) for n in (1, 2, 3)]
        first, _ = self.run_script(pulls, all_groups=True)
        for _ in range(5):
            again, _ = self.run_script(pulls, all_groups=True)
            self.assertEqual(again, first)

    # ---- credentials and trust ----------------------------------------------

    def test_talks_to_github_with_the_historical_basic_auth(self):
        """Unset GITHUB_API_URL is the production path and must not change.

        requests turns an auth= tuple into HTTP Basic. It is not what GitHub
        documents, but it is what every builder has been sending, so switching
        it wholesale would change the credential every production request
        carries.
        """
        os.environ.pop("GITHUB_API_URL", None)
        self.run_script([make_pr(1, "01")])
        seen = self.transport_kwargs
        self.assertEqual(seen.get("url"), "https://api.github.com/graphql")
        self.assertEqual(seen.get("auth"), ("bearer", "not-a-real-token"))
        self.assertIsNone(seen.get("headers"))

    def test_talks_to_a_broker_with_a_bearer_header(self):
        """With GITHUB_API_URL set we are behind a credential broker, which
        reads the token to swap out of an Authorization: Bearer header and
        cannot recognise HTTP Basic."""
        os.environ["GITHUB_API_URL"] = "http://127.0.0.1:9999/github"
        try:
            self.run_script([make_pr(1, "01")])
        finally:
            os.environ.pop("GITHUB_API_URL", None)
        seen = self.transport_kwargs
        self.assertEqual(seen.get("url"), "http://127.0.0.1:9999/github/graphql")
        self.assertEqual(seen.get("headers"),
                         {"Authorization": "Bearer not-a-real-token"})
        self.assertIsNone(seen.get("auth"))

    def test_trust_collaborators_widens_who_may_be_built(self):
        """TRUST_COLLABORATORS decides whether a first-time contributor's code
        runs on our builders, so it is worth pinning both ways."""
        pull = make_pr(1, "01", association="CONTRIBUTOR", approved=False,
                       reviewed=False)
        rows, _ = self.run_script([pull])
        self.assertEqual(rows, [], "a CONTRIBUTOR must not be built by default")

        self.write_check_env("TRUST_COLLABORATORS=true\n")
        rows, _ = self.run_script([pull])
        self.assertEqual([row[1] for row in rows], ["1"])

    def test_trusted_users_are_built_without_review(self):
        pull = make_pr(1, "01", association="NONE", approved=False,
                       reviewed=False)
        self.write_check_env("TRUSTED_USERS=nobody,someone\n")
        rows, _ = self.run_script([pull])
        self.assertEqual([row[1] for row in rows], ["1"])

    def test_a_check_without_the_required_variables_is_skipped(self):
        """A malformed *.env must drop that check, not abort the whole run."""
        with open(os.path.join(self.definitions, "role", "container",
                               "broken.env"), "w") as envf:
            envf.write("PACKAGE=nothing\n")     # no CHECK_NAME, no PR_REPO
        rows, _ = self.run_script([make_pr(1, "01")])
        self.assertEqual([row[1] for row in rows], ["1"])

    def test_priority_label_jumps_the_queue(self):
        pulls = [make_pr(1, "01"), make_pr(2, "02"),
                 make_pr(3, "03", labels=(self.script.PRIORITY_LABEL,))]
        rows, _ = self.run_script(pulls)
        self.assertEqual([row[1] for row in rows], ["3", "1", "2"])

    def test_without_the_label_order_is_unchanged(self):
        pulls = [make_pr(1, "01"), make_pr(2, "02"),
                 make_pr(3, "03", labels=("bug", "enhancement"))]
        rows, _ = self.run_script(pulls)
        self.assertEqual([row[1] for row in rows], ["1", "2", "3"])

    def test_least_broken_prs_are_built_first(self):
        """A PR already red everywhere else is the least useful thing to build:
        its failure would say nothing about this platform, and the red we post
        lands on someone's PR for nothing."""
        pulls = [make_pr(1, "01", broken=9), make_pr(2, "02", broken=0),
                 make_pr(3, "03", broken=3)]
        rows, _ = self.run_script(pulls)
        self.assertEqual([row[1] for row in rows], ["2", "3", "1"])

    def test_the_priority_label_still_beats_a_clean_pr(self):
        """Ordering by breakage must not override an explicit human decision."""
        pulls = [make_pr(1, "01", broken=0),
                 make_pr(2, "02", broken=9, labels=(self.script.PRIORITY_LABEL,))]
        rows, _ = self.run_script(pulls)
        self.assertEqual([row[1] for row in rows], ["2", "1"])

    def test_our_own_red_does_not_push_a_pr_down_the_queue(self):
        """The check's own verdict is excluded from the count. Were it included,
        a PR this check failed would sink a little further every round and never
        be retried -- and the group already carries that verdict anyway."""
        pulls = [make_pr(11, "01", "FAILURE", "10"),
                 make_pr(12, "02", "SUCCESS", "20")]
        rows, _ = self.run_script(pulls, all_groups=True)
        self.assertEqual([row[1] for row in rows], ["11", "12"])

    def test_equally_broken_prs_keep_the_old_order(self):
        """Breakage that is repo-wide (alidist has two such checks) is a
        constant offset, so it cancels out and oldest-first still decides."""
        pulls = [make_pr(1, "03", broken=2), make_pr(2, "01", broken=2),
                 make_pr(3, "02", broken=2)]
        rows, _ = self.run_script(pulls)
        self.assertEqual([row[1] for row in rows], ["2", "3", "1"])

    def test_all_groups_orders_rebuilds_stalest_first(self):
        """Merged across failed and succeeded, so a stale green PR outranks a
        freshly rebuilt red one -- what replaced the 70/30 coin flip."""
        pulls = [
            make_pr(11, "01", "FAILURE", "10"),
            make_pr(12, "02", "FAILURE", "20"),
            make_pr(13, "03", "SUCCESS", "05"),   # stalest of all
            make_pr(14, "04", "SUCCESS", "25"),   # freshest of all
        ]
        rows, _ = self.run_script(pulls, all_groups=True)
        self.assertEqual([row[1] for row in rows], ["13", "11", "12", "14"])


if __name__ == "__main__":
    unittest.main()
