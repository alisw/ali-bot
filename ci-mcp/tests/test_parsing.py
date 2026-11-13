"""Log parsing in ci_mcp_server, against fixtures in the real log formats.

The fixtures copy the line formats of claim-builder.sh, build-one.sh and
build-loop.sh as they appear in a claim worker's Nomad logs: stdout carries the
ROUND banners as text, stderr carries them as `set -x` traces of the printf.

Run with: .venv/bin/python -m unittest discover -s tests
"""

import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ci_mcp_server as m            # noqa: E402


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name)) as source:
        return source.read()


STDERR = fixture("claim-stderr.log")
STDOUT = fixture("claim-stdout.log")
ALLOC_ID = "68b69499-3819-d995-5671-8d390694245b"
ALLOC = {
    "ID": ALLOC_ID, "NodeName": "alimetal02.cern.ch", "ClientStatus": "running",
    "TaskStates": {"ci": {"State": "running", "Restarts": 1,
                          "StartedAt": "2026-11-13T14:17:26.780939876Z",
                          "LastRestart": "2026-11-13T15:17:26.72349199+01:00"}},
}


def fake_log(stdout=STDOUT, stderr=STDERR):
    def nomad_log(alloc_id, task, stream="stderr", max_bytes=0):
        return (stdout if stream == "stdout" else stderr), False
    return nomad_log


class SinceTest(unittest.TestCase):
    lines = STDERR.splitlines()

    def test_forms(self):
        for since, want in [("15:20", ("2026-11-13", "15:20:00")),
                            ("9:05:07", ("2026-11-13", "09:05:07")),
                            ("2026-11-13 15:20:00", ("2026-11-13", "15:20:00")),
                            ("2026-11-12 15:20", ("2026-11-12", "15:20:00")),
                            ("2026-11-13@15:20", ("2026-11-13", "15:20:00")),
                            ("2026-11-13T15:20:01", ("2026-11-13", "15:20:01"))]:
            self.assertEqual(m.parse_since(since, self.lines), want, since)

    def test_rejects_rather_than_concatenates(self):
        # Used to come out as ("2026-11-12", "2026-11-13 15:20:00").
        for since in ("2026-11-13T14:20:00Z", "yesterday", "15h20"):
            with self.assertRaises(ValueError):
                m.parse_since(since, self.lines)

    def test_full_timestamp_selects_a_suffix(self):
        lines, start, label = m.scope_lines(self.lines, "2026-11-13 15:21", "all")
        self.assertNotIn("WARNING", label)
        self.assertIn("since 2026-11-13 15:21:00", label)
        self.assertTrue(lines[0].endswith("15318 fcdecb021234567890abcdef1234567890abcdef untested"))

    def test_since_beats_the_default_round(self):
        lines, start, label = m.scope_lines(self.lines, "15:21", "last")
        self.assertIn("since", label)
        self.assertTrue(any("1500/4771" in raw for raw in lines))
        self.assertTrue(any("15318" in raw for raw in lines))


class ClaimsTest(unittest.TestCase):
    def classes(self, text):
        claims = m.find_claims(text.splitlines())
        return [(c["pr"], m.claim_class(c, i + 1 == len(claims)))
                for i, c in enumerate(claims)]

    def test_stderr_xtrace(self):
        self.assertEqual(self.classes(STDERR), [
            ("15000", "no-END"), ("18724", "FAILED rc=1"), ("16121", "claim-lost"),
            ("4966", "status-cap"), ("15318", "verdict-only"),
            ("18701", "stale-skip"), ("16114", "running")])

    def test_stdout_text(self):
        # Same rounds; the status-cap skip is only visible on stderr.
        self.assertEqual(self.classes(STDOUT), [
            ("15000", "no-END"), ("18724", "FAILED rc=1"), ("16121", "claim-lost"),
            ("4966", "ok"), ("15318", "verdict-only"),
            ("18701", "stale-skip"), ("16114", "running")])

    def test_merged_streams_count_once(self):
        merged = "\n".join(a + "\n" + b for a, b in zip(
            [l for l in STDERR.splitlines() if "printf '%s ===== ROUND" in l],
            [l for l in STDOUT.splitlines() if "===== ROUND" in l]))
        self.assertEqual(len(m.find_claims(merged.splitlines())), 7)


class RoundScopeTest(unittest.TestCase):
    lines = STDERR.splitlines()

    def test_round_stops_at_its_end(self):
        # The code-check round for #18724 used to run on into the alidist#4966
        # round, because only the former printed a ci-round: block.
        lines, _, label = m.scope_lines(self.lines, "", "2")
        self.assertIn("round=2: build/O2Physics/code-check on "
                      "AliceO2Group/O2Physics#18724", label)
        self.assertTrue(any("code-check says no" in raw for raw in lines))
        self.assertFalse(any("4966" in raw or "status limit" in raw for raw in lines))

    def test_last_is_last_held_claim(self):
        lines, _, label = m.scope_lines(self.lines, "", "last")
        self.assertIn("round=6: build/O2/fullCI-slc10-x86 on pr=16114@aaaaaaaa", label)
        self.assertEqual([raw for raw in lines if "/4771]" in raw or "/642]" in raw],
                         [self.lines[-1]])

    def test_errors_scoped_to_round(self):
        with mock.patch.object(m, "nomad_log", fake_log()), \
             mock.patch.object(m, "_resolve_alloc", lambda a: ALLOC_ID):
            self.assertIn("code-check says no", m.ci_build_errors("68b6", round="2"))
            self.assertNotIn("code-check says no",
                             m.ci_log_search("68b6", "error", round="3"))


class RoundsToolTest(unittest.TestCase):
    def test_one_line_per_round(self):
        with mock.patch.object(m, "nomad_log", fake_log()), \
             mock.patch.object(m, "_resolve_alloc", lambda a: ALLOC_ID):
            out = m.ci_rounds("68b6").splitlines()
            lost = m.ci_rounds("68b6", show_lost=True).splitlines()
        self.assertTrue(out[0].startswith("6 round(s) in 68b69499 ("))
        self.assertIn("1 claim-lost", out[0])
        self.assertEqual(len(out), 1 + 6)
        self.assertEqual(len(lost), 1 + 7)
        self.assertIn("[lost]", lost[3])      # lost claims are not numbered
        row = {line.split()[0]: line for line in out[1:]}
        self.assertIn("FAILED rc=1", row["[round=2]"])
        self.assertIn("2m00s", row["[round=2]"])
        self.assertIn("alidist@d7c73c54be20", row["[round=2]"])
        self.assertIn("status-cap", row["[round=3]"])
        self.assertIn("verdict-only", row["[round=4]"])
        self.assertIn("stale-skip", row["[round=5]"])
        self.assertIn("running", row["[round=6]"])
        self.assertIn("no-END", row["[round=1]"])


class WorkersTest(unittest.TestCase):
    def run_workers(self, stdout, stderr=STDERR):
        jobs = [{"ID": "ci-linux-x86"}]
        with mock.patch.object(m, "nomad_log", fake_log(stdout, stderr)), \
             mock.patch.object(m, "nomad_get",
                               lambda path: jobs if path == "/v1/jobs" else [ALLOC]):
            return m.ci_workers()

    def test_current_round_not_last_pr(self):
        out = self.run_workers(STDOUT)
        self.assertIn("build/O2/fullCI-slc10-x86 pr=16114@aaaaaaaa type=failed "
                      "since 15:23:00", out)
        self.assertIn("[1500/4771]", out)
        self.assertNotIn("642", out)

    def test_progress_only_after_begin(self):
        # Like af5ea582: a new round with no progress yet must not show the
        # previous round's [155/642].
        stderr = STDERR.rsplit("\n", 2)[0] + "\n"
        out = self.run_workers(STDOUT, stderr)
        self.assertIn("pr=16114", out)
        self.assertNotIn("[", out.split("pr=16114", 1)[1])

    def test_idle_between_rounds(self):
        stdout = "\n".join(STDOUT.splitlines()[:-2]) + "\n"
        out = self.run_workers(stdout)
        self.assertIn("idle since 15:22:00 (last: staging pr=18701 stale-skip)", out)
        self.assertNotIn("4771", out)

    def test_heartbeat_when_begin_scrolled_out(self):
        stdout = STDOUT.splitlines()[-1] + "\n"
        out = self.run_workers(stdout)
        self.assertIn("build/O2/fullCI-slc10-x86 pr=16114@aaaaaaaa  5m in", out)


class RolloutTest(unittest.TestCase):
    def test_task_start_in_builder_clock(self):
        self.assertEqual(m.task_start(ALLOC["TaskStates"]["ci"]),
                         ("2026-11-13", "15:17:26"))
        never = {"StartedAt": "0001-01-01T00:00:00Z", "LastRestart": None}
        self.assertIsNone(m.task_start(never))
        # Never restarted: no offset from Nomad, so the builders' zone (CET).
        self.assertEqual(m.task_start({"StartedAt": "2026-09-30T05:37:06.0975Z"}),
                         ("2026-09-30", "07:37:06"))

    def test_line(self):
        with mock.patch.object(m, "nomad_log", fake_log()):
            line = m.rollout_line("ci-linux-x86", ALLOC, 8)
        self.assertIn("started 11-13 15:17  restarts=1", line)
        self.assertIn("alisw/ali-bot@fb9457ba", line)
        # Everything before the restart is not counted: the killed round 41,
        # its Traceback and its ModuleNotFoundError.
        self.assertIn("rounds: 1 FAILED, 1 claim-lost, 1 running, 1 stale-skip, "
                      "1 status-cap, 1 verdict-only", line)
        self.assertIn("cooldown=1 traceback=1 ModuleNotFound=0 "
                      "cleanup-impossible=0", line)
        self.assertIn("free=901.50 GiB", line)

    def test_version_unknown_when_start_not_in_window(self):
        stdout = "\n".join(STDOUT.splitlines()[8:])
        with mock.patch.object(m, "nomad_log",
                               lambda *a, **k: (stdout if k.get("stream") == "stdout"
                                                else STDERR, True)):
            line = m.rollout_line("ci-linux-x86", ALLOC, 8)
        self.assertIn("unknown (start not in window)", line)

    def test_crash_looping_task(self):
        alloc = dict(ALLOC, TaskStates={"ci": dict(
            ALLOC["TaskStates"]["ci"], State="pending",
            Events=[{"DisplayMessage": "Exit Code: 1"},
                    {"DisplayMessage": "Exceeded allowed attempts, applying a delay"}])})
        with mock.patch.object(m, "nomad_log", fake_log()):
            line = m.rollout_line("ci-linux-x86-gpu", alloc, 8)
        self.assertIn("TASK pending: Exceeded allowed attempts", line)
        jobs = [{"ID": "ci-linux-x86-gpu"}]
        with mock.patch.object(m, "nomad_log", fake_log()), \
             mock.patch.object(m, "nomad_get",
                               lambda path: jobs if path == "/v1/jobs" else [alloc]):
            out = m.ci_workers()
        self.assertIn("TASK pending", out)
        self.assertNotIn("16114", out)
        self.assertNotIn("4771", out)

    def test_tool(self):
        with mock.patch.object(m, "nomad_log", fake_log()), \
             mock.patch.object(m, "nomad_get", lambda path: [ALLOC]):
            out = m.ci_rollout("ci-linux-x86")
        self.assertEqual(len(out.splitlines()), 1)
        self.assertTrue(out.startswith("ci-linux-x86         68b69499 alimetal02"))


if __name__ == "__main__":
    unittest.main()
