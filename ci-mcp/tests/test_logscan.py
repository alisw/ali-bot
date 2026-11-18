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

    FETCH = ("AliceO2Group__AliceO2__16148__2cd04620ebbed9813af35af236518401fc0dbeb6__"
             "build_O2_gpu-test-slc10-x86__fullLog.txt")

    def test_git_fetch_is_the_reason_not_no_output(self):
        # 2026-11-18: github.com/microsoft/onnxruntime went away, aliBuild failed
        # to update the mirror before any package log existed. report-pr-errors
        # now writes aliBuild's fetch-log.txt into the log.
        raw = self.read(self.FETCH)
        text = raw.decode()
        self.assertIn("Command: git fetch", L.fetch_failures(text))
        self.assertFalse(L.is_no_output(text))
        ex = L.extract_log(raw)
        self.assertEqual(ex["no_output"], 0)
        e = ex["errors"][0]
        self.assertEqual((e["kind"], e["infra"], e["fatal"]), ("git-fetch", 1, 1))
        self.assertIn("Exit code: 128", e["text"])
        self.assertEqual(e["line"], 11)
        # The same failure on another worker: another allocation in the
        # directory, the same signature (the body is not part of it).
        other = text.replace("6fb6f564-0e5b-7ccf-fc08-7ec81c9e25d8", "0123abcd-1111-2222-3333-444455556666")
        self.assertEqual(L.extract_log(other.encode())["errors"][0]["sig_hash"], e["sig_hash"])
        # Without the section, the same log is still no-output.
        bare = L.FETCH_SECTION_RE.sub("", text)
        self.assertTrue(L.is_no_output(bare))

    def test_tool_summary_ranks_below_the_error_it_reports(self):
        head = ("Finished building on ci-efba6f.example.org at 2026-11-16-10:00:00\n"
                "Built commit %s from https://github.com/AliceO2Group/O2Physics/pull/1\n"
                "## Begin BUILD/O2Physics-latest/log\n" % ("f" * 40))
        cc = "/sw/SOURCES/O2Physics/0/PWGHF/Tasks/taskX.cxx:12:3: error: 'fooBar' was not declared in this scope\n"
        ex = L.extract_log((head + cc + "[812/3400] Building CXX object x.o\n"
                            "ninja: build stopped: subcommand failed.\n").encode())
        kinds = [e["sig_exact"] for e in ex["errors"]]
        self.assertIn("was not declared in this scope", kinds[0])
        self.assertTrue(any(k.startswith("ninja: build stopped") for k in kinds[1:]), kinds)
        # ...and the error ninja stopped for is the fatal one, in its package.
        self.assertEqual((ex["errors"][0]["fatal"], ex["failed_package"]), (1, "O2Physics"))
        # The summary alone is still reported.
        only = L.extract_log((head + "ninja: build stopped: subcommand failed.\n").encode())
        self.assertTrue(only["errors"][0]["sig_exact"].startswith("ninja: build stopped"))
        # severity() itself: a summary after every specific error, even an
        # earlier one of a lower tier (a plain compiler error is not FATAL_RE).
        err = L.severity((1, "error: 'x' was not declared", "error", 1))
        ninja = L.severity((1, "ninja: build stopped: subcommand failed.", "error", 2))
        ld = L.severity((1, "collect2: error: ld returned 1 exit status", "error", 3))
        ctest = L.severity((1, "90% tests passed, 1 tests failed out of 10", "test", 3))
        failed = L.severity((1, "Test: testFoo ***Failed", "test", 2))
        self.assertLess(err, ninja)
        self.assertLess(err, ld)
        self.assertLess(failed, ctest)

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


# The DPL trace of a CCDB fetch failing in a digitizer (from a real aarch64 log,
# shortened): the throw site, then the abort's own trace.
DPL_LINUX = """\
./digi.log[960:internal-dpl-ccdb-backend]: [01:09:23][FATAL] Alien Token Check failed - Please get an alien token
./digi.log[960:internal-dpl-ccdb-backend]: Executable is /sw/slc9_aarch64/O2/local3/bin/o2-sim-digitizer-workflow
./digi.log[960:internal-dpl-ccdb-backend]:     /sw/slc9_aarch64/O2/local3/lib/libO2Framework.so:     ?? ??:0
./digi.log[960:internal-dpl-ccdb-backend]:     /sw/slc9_aarch64/FairLogger/v2.3.1-6/lib/libFairLogger.so.2.3: fair::Logger::~Logger()
./digi.log[960:internal-dpl-ccdb-backend]:     /sw/slc9_aarch64/O2/local3/lib/libO2CCDB.so: o2::ccdb::CcdbApi::initTGrid() const
./digi.log[960:internal-dpl-ccdb-backend]:     /sw/slc9_aarch64/O2/local3/lib/libO2CCDB.so: o2::ccdb::CcdbApi::loadFileToMemory(std::vector<char, std::pmr::polymorphic_allocator<char> >&, int)
./digi.log[961:TRDDigitizer]: [01:09:27][INFO] TRD: Sending 10411 digits
./digi.log[960:internal-dpl-ccdb-backend]:     /sw/slc9_aarch64/O2/local3/lib/libO2CCDB.so:     ?? ??:0
./digi.log[960:internal-dpl-ccdb-backend]:     /sw/slc9_aarch64/O2/local3/lib/libO2CCDB.so:     ?? ??:0
./digi.log[960:internal-dpl-ccdb-backend]:     /sw/slc9_aarch64/O2/local3/lib/libO2Framework.so: o2::framework::DataProcessingDevice::doRun(o2::framework::ServiceRegistryRef)
./digi.log[960:internal-dpl-ccdb-backend]:     /sw/slc9_aarch64/FairMQ/v1.11.0-7/lib/libfairmq.so.1.11.0: fair::mq::Device::RunWrapper()
./digi.log[960:internal-dpl-ccdb-backend]:     o2-sim-digitizer-workflow:     main at runDataProcessing.h:218
./digi.log[960:internal-dpl-ccdb-backend]:     /lib64/libc.so.6:     ?? ??:0
./digi.log[960:internal-dpl-ccdb-backend]: [01:09:30][ERROR] Exception while running: Fatal error. Rethrowing.
./digi.log[960:internal-dpl-ccdb-backend]: *** Program crashed (Aborted)
./digi.log[960:internal-dpl-ccdb-backend]: Backtrace by DPL:
./digi.log[960:internal-dpl-ccdb-backend]: Executable is /sw/slc9_aarch64/O2/local3/bin/o2-sim-digitizer-workflow
./digi.log[960:internal-dpl-ccdb-backend]:     linux-vdso.so.1:     ?? ??:0
./digi.log[960:internal-dpl-ccdb-backend]:     /sw/slc9_aarch64/GCC-Toolchain/v14.2.0-alice2-1/lib64/libstdc++.so.6(_ZN9__gnu_cxx27__verbose_terminate_handlerEv+0x184) [0xff51e0cb37c4]:     ?? ??:0
./digi.log[960:internal-dpl-ccdb-backend]:     /sw/slc9_aarch64/O2/local3/lib/libO2Framework.so:     ?? ??:0
./digi.log[960:internal-dpl-ccdb-backend]:     /sw/slc9_aarch64/O2/local3/lib/libO2Framework.so: doDPLException(o2::framework::RuntimeErrorRef&, char const*)
./digi.log[960:internal-dpl-ccdb-backend]:     /sw/slc9_aarch64/O2/local3/lib/libO2Framework.so: callMain(int, char**, int (*)(int, char**))
./digi.log[960:internal-dpl-ccdb-backend]:     o2-sim-digitizer-workflow() [0x469bf0]:     _start at ??:?
./digi.log[960:internal-dpl-ccdb-backend]: Backtrace complete.
./digi.log[ERROR] Workflow crashed - PID 960 (internal-dpl-ccdb-backend) did not exit correctly however it's not clear why. Exit code forced to 128.
"""

# The same printer on macOS: backtrace_symbols() "N  lib  0xADDR", then the
# demangled name (BacktraceHelpers.cxx, __APPLE__ branch).
DPL_MAC = """\
[4242:its-tracker_0]: Executable is /Volumes/build/sw/osx_arm64/O2/16084-local2/bin/o2-its-reco-workflow
[4242:its-tracker_0]:     1   libO2Framework.dylib                0x0000000104d2c3a8 : o2::framework::BacktraceHelpers::demangled_backtrace_symbols(void**, unsigned int, int)
[4242:its-tracker_0]:     2   libsystem_platform.dylib            0x0000000189a1e584 : _sigtramp
[4242:its-tracker_0]:     3   libO2ITStracking.dylib              0x0000000110a1b2c4 : o2::its::TrackerTraits<7>::computeLayerTracklets(int, int, int)
[4242:its-tracker_0]:     4   libO2ITStracking.dylib              0x0000000110a1c000 : o2::its::Tracker::clustersToTracks(std::function<void (std::string)>&, std::function<void (std::string)>&)
[4242:its-tracker_0]:     5   libO2Framework.dylib                0x0000000104d00000 : o2::framework::DataProcessingDevice::doRun(o2::framework::ServiceRegistryRef)
[4242:its-tracker_0]:     6   dyld                                0x0000000189a00000 : start
[ERROR] Workflow crashed - PID 4242 (its-tracker_0) was killed abnormally with Segmentation fault
"""

# ROOT's crash handler (etc/gdb-backtrace.sh): all threads, the crashed one has
# the signal handler frames, then the hint section repeats the useful frames.
ROOT_GDB = """\
 *** Break *** segmentation violation



===========================================================
There was a crash (kSigSegmentationViolation).
This is the entire stack trace of all threads:
===========================================================

Thread 2 (Thread 0x7f2a3c1ff640 (LWP 4711) "root.exe"):
#0  0x00007f2a4a8e3c3f in __GI___clock_nanosleep () from /lib64/libc.so.6
#1  0x00007f2a4a8e8b27 in nanosleep () from /lib64/libc.so.6
#2  0x00007f2a4b0a1234 in TTimer::Notify() () from /sw/slc9_x86-64/ROOT/v6-36/lib/libCore.so

Thread 1 (Thread 0x7f2a4a6d7b80 (LWP 4710) "root.exe"):
#0  0x00007f2a4a8f5a7a in wait4 () from /lib64/libc.so.6
#1  0x00007f2a4a86b0f7 in do_system () from /lib64/libc.so.6
#2  0x00007f2a4b1e43a3 in TUnixSystem::StackTrace() () from /sw/slc9_x86-64/ROOT/v6-36/lib/libCore.so
#3  0x00007f2a4b1e1d55 in TUnixSystem::DispatchSignals(ESignals) () from /sw/slc9_x86-64/ROOT/v6-36/lib/libCore.so
#4  <signal handler called>
#5  0x00007f2a38a1b2c4 in o2::tof::Clusterer::processStrip (this=0x55d1, cluster=..., strip=0x0) at /sw/SOURCES/O2/x/Detectors/TOF/reconstruction/src/Clusterer.cxx:142
#6  0x00007f2a38a1c000 in o2::tof::Clusterer::process<o2::tof::DataReader> (this=0x55d1, reader=...) at Clusterer.cxx:61
#7  0x00007f2a38a1d000 in run_clus_tof (inputfile=...) at run_clus_tof.C:44
#8  0x00007f2a4b9a0000 in ?? ()
#9  0x00007f2a4b500000 in TCling::ProcessLine(char const*, TInterpreter::EErrorCode*) () from /sw/slc9_x86-64/ROOT/v6-36/lib/libCling.so
===========================================================


The lines below might hint at the cause of the crash. If you see question
#5  0x00007f2a38a1b2c4 in o2::tof::Clusterer::processStrip (this=0x55d1, cluster=..., strip=0x0) at Clusterer.cxx:142
#6  0x00007f2a38a1c000 in o2::tof::Clusterer::process<o2::tof::DataReader> (this=0x55d1, reader=...) at Clusterer.cxx:61
===========================================================
"""


class CrashTest(unittest.TestCase):
    def test_dpl_linux(self):
        c = L.crashes(DPL_LINUX.splitlines())
        self.assertEqual(len(c), 1, "the abort's own trace has nothing named: no second fingerprint")
        self.assertEqual(c[0]["sig_exact"],
                         "crash in internal-dpl-ccdb-backend (o2-sim-digitizer-workflow): "
                         "o2::ccdb::CcdbApi::initTGrid < o2::ccdb::CcdbApi::loadFileToMemory < [libO2CCDB]")
        self.assertEqual(c[0]["sig_class"],
                         "crash: o2::ccdb::CcdbApi::initTGrid < o2::ccdb::CcdbApi::loadFileToMemory < [libO2CCDB]")
        self.assertEqual(c[0]["position"], 1)
        self.assertTrue(c[0]["text"].startswith("Alien Token Check failed"), c[0]["text"])

    def test_dpl_mac(self):
        c = L.crashes(DPL_MAC.splitlines())
        self.assertEqual([x["sig_exact"] for x in c],
                         ["crash in its-tracker_N (o2-its-reco-workflow): "
                          "o2::its::TrackerTraits<>::computeLayerTracklets < o2::its::Tracker::clustersToTracks"])

    def test_root_gdb(self):
        c = L.crashes(ROOT_GDB.splitlines())
        self.assertEqual([x["sig_exact"] for x in c],
                         ["crash (kSigSegmentationViolation): o2::tof::Clusterer::processStrip < "
                          "o2::tof::Clusterer::process<> < run_clus_tof < [??]"])
        self.assertEqual(c[0]["sig_class"], "crash: o2::tof::Clusterer::processStrip < "
                         "o2::tof::Clusterer::process<> < run_clus_tof < [??]")

    def test_operator_frames_lose_their_arguments(self):
        for raw, name in [
                ("std::operator<< <char, std::char_traits<char> > (__os=..., __str=...) at basic_string.h:6",
                 "std::operator<< <>"),
                ("o2::Foo::operator() (this=0x1, x=2) at Foo.cxx:3", "o2::Foo::operator()"),
                ("std::vector<int, std::allocator<int> >::operator[] (this=0x2, n=3)", "std::vector<>::operator[]"),
                ("Foo::operator->() const", "Foo::operator->"),
                ("o2::Bar<std::map<int, int> >::run(int) + 12", "o2::Bar<>::run")]:
            self.assertEqual(L._frame_symbol(raw), name)

    def test_same_crash_other_addresses_and_pids(self):
        a = L.crashes(DPL_LINUX.splitlines())[0]
        other = (DPL_LINUX.replace("[960:", "[1234:").replace("0xff51e0cb37c4", "0xaaaa00001111")
                 .replace("v1.11.0-7", "v1.11.0-9").replace("local3", "local7"))
        self.assertEqual(L.crashes(other.splitlines())[0]["sig_hash"], a["sig_hash"])

    def test_no_trace_no_crash(self):
        self.assertEqual(L.crashes(["#1 in the list", "#2  0x1234 in foo () from /lib/libx.so", "done"]), [],
                         "gdb-looking lines without a signal handler are not a crash")

    def test_extract_log_ranks_crash_and_points_at_lines(self):
        log = ("Finished building on host at 2026-11-17-10:00:00\n"
               "/sw/SOURCES/O2/x/0/Foo.cxx:12:3: error: no member named 'bar' in 'Foo'\n" + DPL_LINUX)
        ex = L.extract_log(log.encode())
        kinds = [(e["kind"], e["line"]) for e in ex["errors"]]
        self.assertIn(("crash", 4), kinds)              # its "Executable is" line, 1-based
        self.assertIn(("error", 2), kinds)
        crash = next(e for e in ex["errors"] if e["kind"] == "crash")
        self.assertTrue(crash["fatal"])
        self.assertEqual(crash["sig_hash"], L.sig_hash(crash["sig_exact"]))
