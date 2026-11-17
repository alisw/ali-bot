"""logscan.py: secrets scrubber, signatures, header, extraction.

Run with: .venv/bin/python -m unittest discover -s tests
(The end-to-end use is in ../cocoindex/test/cif_test.py.)
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import logscan as L                  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.dirname(HERE)), "cocoindex", "test", "fixtures", "ci-logs")


class ScrubTest(unittest.TestCase):
    CASES = [
        ("token ghp_" + "a1B2c3D4e5" * 4 + " end", "ghp_"),
        ("gho_" + "Z" * 36, "gho_"),
        ("export GH=github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstu", "github_pat_"),
        ("git clone https://oauth2:glpat-" + "Zx9Yw8Vu7T" * 2 + "@gitlab.cern.ch/x.git", "glpat-"),
        ("aws AKIAIOSFODNN7EXAMPLE used", "AKIAIOSFODNN7EXAMPLE"),
        ("Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.payload.sig", "eyJhbGci"),
        ("curl -H 'authorization: token abc123def456'", "abc123def456"),
        ("password=hunter2 and more", "hunter2"),
        ("DB_PASSWORD: 's3cr3t value'", "s3cr3t value"),
        ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG", "wJalrXUtnFEMI"),
        ("api_key=0123456789abcdef", "0123456789abcdef"),
        ("fetch https://alice:pa55w0rd@host.cern.ch/repo.git", "pa55w0rd"),
        ("fetch https://x-access-token" + "Q" * 20 + "@github.com/o/r", "x-access-token"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7bq98\n-----END RSA PRIVATE KEY-----", "MIIEowIBAAKC"),
        ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n(truncated", "b3BlbnNzaC1"),
        ("-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQOYBF0abcdef\n-----END PGP PRIVATE KEY BLOCK-----", "lQOYBF0abcdef"),
        ("key AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q" + " used", "AIzaSyA1b2C3"),
        ("slack xoxb-" + "123456789012-abcdefABCDEF", "xoxb-123456789012"),
        ("OPENAI sk-" + "proj-a1B2c3D4e5F6g7H8i9J0" + " set", "sk-proj-a1B2"),
        ("jwt eyJhbGciOiJSUzI1NiJ9" + ".eyJzdWIiOiJ0ZXN0LXVzZXIifQ" + ".c2lnbmF0dXJlLXZhbHVl ok", "eyJzdWIiOiJ0"),
    ]

    def test_report_names_the_kind(self):
        text, kinds = L.scrub_report("a ghp_" + "a1B2c3D4e5" * 4 + " b AKIAIOSFODNN7EXAMPLE")
        self.assertEqual(sorted(kinds), ["aws-key", "github-token"])
        self.assertEqual(L.scrub_report("nothing here")[1], [])

    def test_each_kind_is_redacted(self):
        for text, secret in self.CASES:
            out = L.scrub(text)
            self.assertNotIn(secret, out, text)
            self.assertIn(L.REDACTED, out, text)

    def test_ordinary_lines_untouched(self):
        for line in ["/sw/SOURCES/O2/x/0/Foo.cxx:12:3: error: 'token' was not declared in this scope",
                     "https://github.com/AliceO2Group/AliceO2/pull/15895",
                     "ninja: build stopped: subcommand failed.",
                     "PWGLF/Tasks/a.cxx:1:2: error: use of undeclared identifier 'passwordHash'",
                     "disk-full on task-runner-0123456789abcdefghij (no sk- key here)"]:
            self.assertEqual(L.scrub(line), line)

    def test_real_fixtures_are_clean(self):
        for f in os.listdir(FIX):
            with open(os.path.join(FIX, f), encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            self.assertEqual(L.scrub(text), text, f)


class SignatureTest(unittest.TestCase):
    CAST = ("C-style casts are discouraged; use static_cast "
            "[google-readability-casting,-warnings-as-errors]")

    def test_prefix_line_col_stripped(self):
        a = L.signature("/sw/SOURCES/O2Physics/slc9_x86-64-slc9_x86-64/0/PWGLF/Tasks/Strangeness/"
                        "lambdaspincorrderived.cxx:531:17: error: " + self.CAST, "error")
        b = L.signature("PWGLF/Tasks/Strangeness/lambdaspincorrderived.cxx:12:3: error: " + self.CAST, "error")
        c = L.signature("/Volumes/build/alice-ci-claim-workdir/osx_arm64/o2physics/sw/SOURCES/O2Physics/18001/0/"
                        "PWGLF/Tasks/Strangeness/lambdaspincorrderived.cxx:9:9: error: " + self.CAST, "error")
        self.assertEqual(a["sig_hash"], b["sig_hash"])
        self.assertEqual(a["sig_hash"], c["sig_hash"])
        self.assertEqual(a["file"], "PWGLF/Tasks/Strangeness/lambdaspincorrderived.cxx")
        self.assertEqual(a["package"], "O2Physics")
        self.assertEqual(a["sig_class"], "error: C-style casts are discouraged; use static_cast "
                                         "[google-readability-casting]")

    def test_class_groups_files_and_names(self):
        a = L.signature("A/x.cxx:1:1: error: unused variable 'cpa' [-Werror,-Wunused-variable]", "error")
        b = L.signature("B/y.cxx:7:2: error: unused variable 'chi2PCA' [-Werror,-Wunused-variable]", "error")
        self.assertNotEqual(a["sig_hash"], b["sig_hash"])
        self.assertEqual(a["class_hash"], b["class_hash"])
        self.assertEqual(a["sig_class"], "error: unused variable '_' [-Wunused-variable]")

    def test_volatile_parts(self):
        a = L.signature("2026-11-10T12:02:11.072+0100 [41817:128179452840000] [executioncontext.cpp:739] [ERROR] "
                        "Failed to download output blob d994727067293738d46e5f11069eb81e9846aa046cd9fdce848840afb93485"
                        "88: 13: Blob absent", "remote-cache")
        b = L.signature("2026-10-20T10:01:02.000+0200 [1:2] [executioncontext.cpp:739] [ERROR] Failed to download "
                        "output blob 4a1af663d2073f48fc5a80eb489846c0c864dbba02e1aebca3f41eab8cdd51ae: 13: Blob absent",
                        "remote-cache")
        self.assertEqual(a["sig_hash"], b["sig_hash"], (a["sig_exact"], b["sig_exact"]))
        t = L.signature("106/583 Test #125: CCDB/test/testCcdbApiHeaders.cxx ..........***Failed    3.23 sec", "test")
        u = L.signature("371/617 Test #128: CCDB/test/testCcdbApiHeaders.cxx ......***Failed    0.96 sec", "test")
        self.assertEqual(t["sig_hash"], u["sig_hash"])

    def test_cmake_body_is_part_of_the_signature(self):
        head = "CMake Error at /sw/slc10_x86-64/CMake/v4.1.4-4/share/cmake-4.1/Modules/FindX.cmake:227 (message):"
        a = L.signature(head + "\n      Could NOT find GLUT (missing: GLUT_glut_LIBRARY)", "cmake")
        b = L.signature(head + "\n      Could NOT find GIF (missing: GIF_LIBRARY)", "cmake")
        self.assertNotEqual(a["sig_hash"], b["sig_hash"])
        self.assertIn("CMake:share/cmake-4.1/Modules/FindX.cmake", a["sig_exact"])


class LogTest(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(FIX, name), "rb") as fh:
            return fh.read()

    def test_no_output(self):
        ex = L.extract_log(self.read("AliceO2Group__AliceO2__15838__29cf2788a8a90fece24e11fdf73a8b1d929a8e69__"
                                     "build_O2_fullCI_slc9__fullLog.txt"))
        self.assertEqual(ex["no_output"], 1)
        self.assertEqual(ex["errors"][0]["kind"], "no-output")
        self.assertEqual(ex["header"]["host"], "ci-efba6f.example.org")
        self.assertEqual(ex["header"]["alloc"], "f2ebe038-f6b8-efdb-3d35-de6467e09fdf")
        # Written before report-pr-errors recorded the alidist commit.
        self.assertIsNone(ex["header"]["alidist"])

    def test_alidist_header(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        text = ("Finished building on ci-efba6f.example.org at 2026-11-16-10:00:00\n"
                "Built commit %s from https://github.com/AliceO2Group/AliceO2/pull/1\n"
                "Built with alidist %s\n" % ("f" * 40, sha))
        self.assertEqual(L.parse_header(text)["alidist"], sha)
        # Only a full sha on its own line counts.
        self.assertIsNone(L.parse_header(text.replace(sha, sha[:12]))["alidist"])

    def test_glut_is_infra(self):
        ex = L.extract_log(self.read("AliceO2Group__AliceO2__15895__3bde6afa67f83f220e3f9e8e4d9bb46d552ec49f__"
                                     "build_O2_gpu-test-slc10-x86__fullLog.txt"))
        kinds = {e["kind"] for e in ex["errors"]}
        self.assertIn("missing-system-dep", kinds)
        self.assertEqual(ex["failed_package"], "O2")
        self.assertEqual(ex["header"]["sha"], "3bde6afa67f83f220e3f9e8e4d9bb46d552ec49f")
        self.assertEqual(ex["header"]["ref"], "15895")

    def test_disk_full(self):
        ex = L.extract_log(self.read("AliceO2Group__O2Physics__17992__a4eb29b6b63777c1a871b1f0e1a1a76466211e09__"
                                     "build_O2Physics_o2_macOS-arm__fullLog.txt"))
        self.assertEqual(ex["errors"][0]["kind"], "disk-full")


if __name__ == "__main__":
    unittest.main()
