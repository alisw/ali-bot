"""Pin what list-release-tags offers, and that build-one.sh then builds it.

The two are separate scripts with one contract between them: for a rebuild row
(failed, succeeded) the lister's WAITING_SINCE is the createdAt of the status
the row was listed for, and build-one.sh's stale_listing skips the row only if
the status it re-reads is NEWER than that. list-release-tags used to pass the
tag's commit date instead, which every verdict on that tag postdates -- so a
failed release was offered every round and stale-skipped every round, never
rebuilt (the O2PDPSuite-daily-20261114 dailies). These tests run the real
lister against a fake GitHub API and feed its output to the real stale_listing.
"""

import calendar
import datetime
import http.server
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LISTER = os.path.join(REPO, "list-release-tags")
HELPERS = os.path.join(REPO, "ci", "build-helpers.sh")

SHA = "022f96fe859754b391356f7d90aac45be5d28f88"
CHECK = "release/O2PDPSuite/daily-slc10-x86"
TAG_TIME = "2026-11-14T00:05:29Z"
STATUS_TIME = "2026-11-14T01:51:07Z"


def epoch(ts):
    return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


class FakeGitHub(http.server.BaseHTTPRequestHandler):
    statuses = []          # set per test

    def do_GET(self):
        today = datetime.date.today().strftime("%Y%m%d")
        if "/git/matching-refs/tags/" in self.path:
            body = [{"ref": f"refs/tags/O2PDPSuite-daily-{today}-0000",
                     "object": {"sha": SHA, "type": "commit"}}]
        elif self.path.endswith(f"/commits/{SHA}/status"):
            body = {"statuses": self.statuses}
        elif self.path.endswith(f"/commits/{SHA}"):
            body = {"commit": {"committer": {"date": TAG_TIME}}}
        else:
            self.send_error(404)
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class ListReleaseTagsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), FakeGitHub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        d = os.path.join(self.work.name, "ali-bot", "ci", "repo-config", "role", "cont")
        os.makedirs(d)
        with open(os.path.join(d, "daily.env"), "w") as f:
            f.write(f"CHECK_NAME={CHECK}\nPR_REPO=alisw/alidist\n"
                    "TAG_REGEX='^O2PDPSuite-daily-%(date)-(?P<time>[0-9]{4})$'\n")

    def tearDown(self):
        self.work.cleanup()

    def list_rows(self, statuses, rebuild_tags=""):
        FakeGitHub.statuses = statuses
        env = dict(os.environ, MESOS_ROLE="role", CUR_CONTAINER="cont",
                   GITHUB_TOKEN="x", REBUILD_TAGS=rebuild_tags,
                   GITHUB_API_URL=f"http://127.0.0.1:{self.server.server_port}",
                   PATH=os.path.join(REPO, "ci") + os.pathsep + os.environ["PATH"])
        p = subprocess.run([LISTER, "-c", "cont"], cwd=self.work.name, env=env,
                           capture_output=True, text=True, check=True)
        return [line.split() for line in p.stdout.splitlines()]

    def stale(self, build_type, since, state, created):
        p = subprocess.run(["bash", "-c", '. "$0"; stale_listing "$@"', HELPERS,
                            build_type, since, state, str(created)],
                           capture_output=True, text=True)
        return p.returncode == 0

    def test_untested_tag_waits_since_the_tag(self):
        rows = self.list_rows([])
        self.assertEqual(len(rows), 1)
        build_type, _tag, sha, env_name, since = rows[0]
        self.assertEqual((build_type, sha, env_name), ("untested", SHA, "daily"))
        self.assertEqual(int(since), epoch(TAG_TIME))

    def test_failed_tag_is_rebuilt_not_stale_skipped(self):
        rows = self.list_rows([{"context": CHECK, "state": "error",
                                "created_at": STATUS_TIME}])
        self.assertEqual(len(rows), 1)
        build_type, _tag, _sha, _env, since = rows[0]
        self.assertEqual(build_type, "failed")
        self.assertEqual(int(since), epoch(STATUS_TIME))
        # build-one.sh re-reads the same status under the claim: not stale.
        self.assertFalse(self.stale("failed", since, "error", epoch(STATUS_TIME)))
        # ...but a verdict posted after the listing still is.
        self.assertTrue(self.stale("failed", since, "error", epoch(STATUS_TIME) + 60))

    def test_forced_success_is_rebuilt(self):
        rows = self.list_rows([{"context": CHECK, "state": "success",
                                "created_at": STATUS_TIME}], rebuild_tags="O2PDPSuite-daily-*")
        self.assertEqual(rows[0][0], "succeeded")
        self.assertFalse(self.stale("succeeded", rows[0][4], "success", epoch(STATUS_TIME)))

    def test_success_and_pending_are_not_offered(self):
        for state in ("success", "pending"):
            self.assertEqual(self.list_rows([{"context": CHECK, "state": state,
                                              "created_at": STATUS_TIME}]), [], state)

    def test_other_contexts_are_ignored(self):
        rows = self.list_rows([{"context": "build/other", "state": "error",
                                "created_at": STATUS_TIME}])
        self.assertEqual(rows[0][0], "untested")


if __name__ == "__main__":
    unittest.main()
