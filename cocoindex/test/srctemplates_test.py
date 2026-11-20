"""srctemplates.py: public templates of runtime messages, from the code that prints them.

    python test/srctemplates_test.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import srctemplates as S  # noqa: E402

failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


SRC = {
    "CCDB/include/CCDB/BasicCCDBManager.h": """
        if (!ptr) {
          reportFatal(std::string("Got nullptr from CCDB for path ") + path + std::string(" and timestamp ") +
                      std::to_string(timestamp));
        }""",
    "CCDB/src/CcdbApi.cxx": """
        LOG(error) << "Requested resource does not exist: " << url;
        LOGP(fatal, "Unable to find CCDB object {}/{}", path, ts);
        LOGF(error, "Device %s (%d) returned with %d", name, pid, code);
        throw std::runtime_error(fmt::format("Cannot open file {} for writing", fname));
        LOG(info) << "short";
        O2_SIGNPOST_EVENT_EMIT_ERROR(device, cid, "Run", "Exception while running: %{public}s. Rethrowing.", err.what);
        // LOG(error) << "A line commented out prints nothing";
        /* LOG(error) << "Nor does a block commented out";
           */ const char* u = "http://not.a/comment"; LOG(warn) << "Message after a block comment";
        std::string printError(int code) const {
          LOG(error) << "A message in the body of a reporting function " << code;
          return "unrelated literal text of the function body";
        }""",
    "CCDB/test/testCcdbApi.cxx": """
        BOOST_AUTO_TEST_CASE(timestamptest)
        {
          BOOST_CHECK(path2 != nullptr);
        }""",
    "CCDB/CMakeLists.txt": "o2_add_test(CcdbApiHeaders SOURCES test/testCcdbApiHeaders.cxx)\n",
    "CCDB/test/testCcdbApiHeaders.cxx": "int main() {}\n",
    "interpreter/llvm/Big.cxx": 'LOG(error) << "never indexed: a skipped directory";\n',
    # An analysis task, its workflow, a CCDB path and the DPL messages naming them.
    "PWGHF/D2H/Tasks/taskD0.cxx": 'struct HfTaskD0 { Configurable<int> cfgUseMl{"cfgUseMl", 1}; int riedel; };\n'
                                  'auto p = "Analysis/PID/TPC/Response";\n',
    "PWGHF/D2H/Tasks/CMakeLists.txt": "o2physics_add_dpl_workflow(hf-task-d0 SOURCES taskD0.cxx COMPONENT_NAME Analysis)\n",
    "Framework/Core/src/runner.cxx": 'LOGP(error, "- Device {}: pid {} (exit {})", name, pid, code);\n'
                                     'LOGP(error, "Aborting because of missing option {}", opt);\n'
                                     'LOGP(error, "Cannot open {} owned by user {}", f, u);\n',
}
ROOTSRC = {
    "hist/hist/src/THnBase.cxx": 'Error("SetTitle", "Trying to assign a title to axis %d for a %d-dimensional '
                                 'histogram.", i, n);\n'
                                 '      Error(where, "%s", msg);\n',
    "geom/geom/src/TGeoPcon.cxx": 'Error("ComputeBBox", "Duplicated section %d/%d for shape %s", i, i + 1, GetName());\n',
    "core/dictgen/src/LinkdefReader.cxx": 'Error("Error: missing ; at end of rule", tok, PP, false);\n',
}

work = Path(tempfile.mkdtemp(prefix="srctemplates-"))
DPGSRC = {
    "test/run_generator_tests.sh": 'echo_red "Test ${test_num}: ${ini_path} with generator ${generator} -> FAILED"\n'
                                   'printf "Workflow %s finished with %d errors\\n" "$wf" "$n"\n',
    "MC/bin/o2dpg_sim_workflow.py": 'print(f"Cannot create workflow: {name} has no generator", file=sys.stderr)\n'
                                    'raise ValueError("Unknown collision system " + system)\n'
                                    "logger.error('Anchoring failed for run %d after %d tries', run, n)\n",
}
# aliBuild's own messages (alibuild_helpers/workarea.py, logged_scm): one
# literal over several lines, %r, written with logf.write, and dieOnError.
ALIBUILDSRC = {
    "alibuild_helpers/workarea.py":
        '      logf.write("%s command for package %r failed.\\n"\n'
        '                 "Command: %s %s\\nIn directory: %s\\nExit code: %d\\n" %\n'
        '                 (scm.name, package, scm.name.lower(), " ".join(command), directory, err))\n'
        '  dieOnError(err, "Error during %s %s for reference repo for %s." %\n'
        '             (scm.name.lower(), command[0], package))\n',
}
# alidist: recipes, whose package names are public (and whose echo lines are no templates)
ALIDISTSRC = {
    "vecgeom.sh": "package: VecGeom\nversion: v1\n---\necho \"Building the VecGeom package now please wait\"\n",
    "hepmc3.sh": "package: HepMC3\nversion: v3\n---\n",
}
for repo, files in (("AliceO2Group/AliceO2", SRC), ("root-project/root", ROOTSRC), ("AliceO2Group/O2DPG", DPGSRC),
                    ("alisw/alibuild", ALIBUILDSRC), ("alisw/alidist", ALIDISTSRC)):
    d = work / repo.replace("/", "__")
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"], check=True)

REPOS = ["AliceO2Group/AliceO2", "root-project/root", "AliceO2Group/O2DPG", "alisw/alibuild", "alisw/alidist"]
idx = S.build(str(work), REPOS)
texts = {"".join("_" if p == S.HOLE else "" if p == S.CLS else p for p in t["pieces"]).strip() for t in idx["templates"]}
print("   ", sorted(texts))
check({"Got nullptr from CCDB for path _ and timestamp _", "Requested resource does not exist: _",
       "Unable to find CCDB object _/_", "Device _ (_) returned with _", "Cannot open file _ for writing",
       "Error in <SetTitle>: Trying to assign a title to axis _ for a _-dimensional histogram.",
       "Exception while running: _. Rethrowing.", "Error: missing ; at end of rule_"} <= texts,
      "LOG <<, LOGP {}, LOGF %, throw fmt::format, string concatenation, ROOT Error(), "
      "a signpost's format, and a local Error(message, ...) give templates")
check(not any("short" in t or "never indexed" in t or t.startswith("Error in <_>") for t in texts),
      "too little literal text, skipped directories and ROOT's Error(where, \"%s\") give none")
check(not any("commented out" in t for t in texts) and "Message after a block comment" in texts,
      "commented-out code gives no template, and the code after a comment still does")
check("A message in the body of a reporting function _" in texts
      and not any("unrelated literal text" in t for t in texts),
      "a function named like a reporter is not a call: its body's own calls count, not the whole body")
line = {"".join("_" if p == S.HOLE else "" if p == S.CLS else p for p in t["pieces"]).strip(): t["line"]
        for t in idx["templates"]}
check(line["Message after a block comment"] == 10, f"line numbers hold across comments ({line['Message after a block comment']})")
check({"Test _: _ with generator _ -> FAILED", "Workflow _ finished with _ errors", "Cannot create workflow: _ has no generator",
       "Unknown collision system _", "Anchoring failed for run _ after _ tries"} <= texts,
      "shell echo/printf and Python print/raise/logging give templates")
check({"_ command for package _ failed.", "Error during _ _ for reference repo for _."} <= texts
      and not any("Exit code" in t for t in texts),
      "aliBuild: one template per line of a multi-line literal (%r included), logf.write and dieOnError; "
      "lines too short to anchor a match dropped")
check(S.build(str(work), REPOS) is None, "same heads: the index is kept")

key = b"k" * 32
T = S.load(str(work), key.decode())
sha = T.heads["AliceO2Group/AliceO2"]
m1 = T.match("error", "[FATAL] Got nullptr from CCDB for path Users/m/meide/Tests/<host>/pid-94602/A and timestamp 1500")
m2 = T.match("error", "[FATAL] Got nullptr from CCDB for path Users/m/meide/Tests/<host>/pid-11/A and timestamp 1700")
m3 = T.match("error", "[FATAL] Got nullptr from CCDB for path Users/x/other/Tests/<host>/pid-11/B and timestamp 1700")
check(m1 and m1["fingerprint"] == "Got nullptr from CCDB for path _ and timestamp _" and m1["fingerprint_kind"] == "source"
      and m1["fingerprint_src"].startswith(f"https://github.com/AliceO2Group/AliceO2/blob/{sha}/"
                                           "CCDB/include/CCDB/BasicCCDBManager.h#L"),
      f"a runtime message gets its template and a permalink to the code ({m1})")
check("meide" not in str(m1), "the values are not in the result")
check(m1["filler_hash"] == m2["filler_hash"] != m3["filler_hash"],
      "pids and timestamps do not split one failure; another CCDB path does")
check(S.load(str(work)).match("error", "[FATAL] Got nullptr from CCDB for path a and timestamp 1")["filler_hash"] is None,
      "without a key, no filler hash (never a plain, guessable one)")
m = T.match("error", "./digi.log[TPCDigitizer_0]: <time>[ERROR] Requested resource does not exist: http://x/ccdb/a")
check(m and m["fingerprint"] == "Requested resource does not exist: _", "device and time prefixes are stripped")
m = T.match("error", "Error in <SetTitle>: Trying to assign a title to axis 5 for a 4-dimensional histogram.")
check(m and m["fingerprint_src"].startswith("https://github.com/root-project/root/blob/"), "ROOT's Error() lines match")
m = T.match("error", 'CCDB/test/testCcdbApi.cxx: error: in "timestamptest": check path2 != nullptr has failed [0 == nullptr]')
check(m and m["fingerprint"] == 'error: in "timestamptest": check path2 != nullptr has failed [_]'
      and m["fingerprint_kind"] == "test" and m["fingerprint_src"].endswith("testCcdbApi.cxx#L4"),
      f"a Boost.Test check found in the named test file ({m})")
m = T.match("error", 'CCDB/test/testCcdbApi.cxx: error: in "timestamptest": check secret_thing == 3 has failed [1 != 3]')
check(m and m["fingerprint"] == 'error: in "timestamptest": check _ has failed [_]' and m["filler_hash"]
      and m["fingerprint_src"].endswith("testCcdbApi.cxx#L2"),
      f"a check no longer in the file (the test changed): its case is kept, the expression is a value ({m})")
check(T.match("error", 'CCDB/test/testCcdbApi.cxx: error: in "nosuchcase": check a == b has failed') is None,
      "a test case not in that file stays hash-only")
m = T.match("error", "./digi.log[internal-dpl-ccdb-backend]: <time>[ERROR] Exception while running: Fatal error. Rethrowing.")
check(m and m["fingerprint"] == "Exception while running: _. Rethrowing.", f"a DPL signpost message ({m})")
m = T.match("error", "Error: missing ; at end of rule at Detectors/TPC/base/src/TPCBaseLinkDef.h")
check(m and m["fingerprint"] == "Error: missing ; at end of rule_" and "LinkdefReader.cxx" in m["fingerprint_src"],
      f"rootcling's own Error(message, ...) ({m})")
m = T.match("test", "Test: CCDB/test/testCcdbApiHeaders.cxx ***Failed")
check(m and m["fingerprint"] == "Test: CCDB/test/testCcdbApiHeaders.cxx ***Failed", f"a ctest name in the tree ({m})")
check(T.match("test", "Test: /home/someone/private_test ***Failed") is None, "a test name not in the tree stays hash-only")
check(T.match("error", "[ERROR] something nobody wrote down") is None, "a message no code prints stays hash-only")
m = T.match("error", "Error in <TGeoPcon::ComputeBBox>: Duplicated section 11/12 for shape TGeoPcon")
check(m and m["fingerprint"] == "Error in <ComputeBBox>: Duplicated section _/_ for shape _",
      f"ROOT's Error() printed with its class prefix ({m})")
m = T.match("error", "Test 90: /O2DPG/MC/config/x.ini with generator External -> FAILED")
check(m and m["fingerprint"] == "Test _: _ with generator _ -> FAILED" and "run_generator_tests.sh#L1" in m["fingerprint_src"],
      f"an O2DPG shell message ({m})")
m = T.match("error", "[qc-sink-QTST_XYZTask_0]: <time> !!! Error - Cannot create workflow: pp has no generator")
check(m and m["fingerprint"] == "Cannot create workflow: _ has no generator", f"InfoLogger's '!!! Error -' prefix ({m})")
for line, want in [("ninja: build stopped: subcommand failed.", "ninja: build stopped: subcommand failed."),
                   ("gmake[2]: *** [src/XrdPosix/CMakeFiles/x.dir/all] Error 2", "make: *** [_] Error _"),
                   ("collect2: error: ld returned 1 exit status", "collect2: error: ld returned _ exit status"),
                   ('<time> [N:N] [buildboxcommon_grpcretrier.cpp:177] [ERROR] Retry limit (0) exceeded for '
                    '"FindMissingBlobs()", last gRPC error was [14: failed]',
                    'Retry limit (_) exceeded for "_", last gRPC error was _'),
                   ("checking pthread.h usability configure: error: C++ compiler missing", "configure: error: _")]:
    m = T.match("error", line)
    check(m and m["fingerprint"] == want and m["fingerprint_kind"] == "tool", f"tool message: {want!r} ({m})")
m = T.match("test", "N% tests passed, N tests failed out of N")
check(m and m["fingerprint"] == "_% tests passed, _ tests failed out of _", "the ctest summary")
m = T.match("error", 'CCDB/test/testCcdbApi.cxx: fatal error: in "timestamptest": critical check p != nullptr has failed')
check(m and m["fingerprint"] == 'fatal error: in "timestamptest": critical check _ has failed', f"a critical check ({m})")
m = T.match("error", 'CCDB/test/testCcdbApi.cxx: error: in "timestamptest": difference{nan} between a{1} and b{2} exceeds 1%')
check(m and m["fingerprint"] == 'error: in "timestamptest": _' and m["filler_hash"], f"another Boost.Test message ({m})")
check(T.match("cmake", "Requested resource does not exist: x") is None, "only error and test kinds are matched")
w = T.match("error", "[ERROR] Exception while running: Requested resource does not exist: http://ccdb/x/y. Rethrowing.")
check(w and w["fingerprint"] == "Requested resource does not exist: _" and "CcdbApi.cxx" in w["fingerprint_src"],
      f"a DPL wrapper carrying a known message: the inner message is the fingerprint ({w and w['fingerprint']})")
w = T.match("error", "[ERROR] Exception while running: something nobody prints anywhere. Rethrowing.")
check(w and w["fingerprint"] == "Exception while running: _. Rethrowing.",
      f"a wrapper carrying an unknown message keeps the wrapper's template ({w and w['fingerprint']})")
h = T.match("error", "ERROR: O2 exit code is 1")
check(h and h["fingerprint"] == "ERROR: O2 exit code is _" and h["fingerprint_kind"] == "tool",
      f"the Hyperloop runner's exit line is a tool message ({h})")
g = T.match("git-fetch", "Git command for package 'ONNXRuntime' failed.")
check(g and g["fingerprint"] == "_ command for package _ failed." and g["fingerprint_kind"] == "source"
      and "alisw/alibuild" in g["fingerprint_src"] and "workarea.py#L1" in g["fingerprint_src"]
      and g["filler_hash"],
      f"logscan's git-fetch (aliBuild could not update a mirror) gets aliBuild's own template ({g})")

# ---- fillers_public: the values that may be shown
import json  # noqa: E402
import sqlite3  # noqa: E402
fp = lambda m: m and m["fillers_public"] and json.loads(m["fillers_public"])  # noqa: E731
T = S.load(str(work), key.decode())
check(fp(T.match("error", "[ERROR] - Device hf-task-d0: pid 4242 (exit 139)")) == [None, None, "139"],
      "without the people list only numbers are shown, and never a pid")
pdb = work / "people.db"
c = sqlite3.connect(pdb)
c.execute("CREATE TABLE commits (author_name, author_email)")
c.executemany("INSERT INTO commits VALUES (?, ?)", [("Martin Eide", "meide@cern.ch"), ("Anton Riedel", "anton.riedel@x")])
c.execute("CREATE TABLE prs (author)")
c.execute("INSERT INTO prs VALUES ('aalkin')")
c.commit()
c.close()
T.people = S.people(str(pdb))
check({"eide", "meide", "riedel", "aalkin"} <= T.people, f"people: surnames, e-mail names, logins ({sorted(T.people)})")
m = T.match("error", "[ERROR] - Device hf-task-d0: pid 4242 (exit 139)")
check(fp(m) == ["hf-task-d0", None, "139"], f"a DPL device: the task's name, its exit code; not the pid ({m})")
check(fp(T.match("error", "[ERROR] - Device o2-analysis-hf-task-d0_1: pid 7 (exit 0)"))[0] == "o2-analysis-hf-task-d0_1",
      "a workflow's executable name and a numbered device")
check(fp(T.match("error", "[ERROR] Aborting because of missing option cfgUseMl")) == ["cfgUseMl"],
      "an option name of the code")
check(not any("VecGeom package" in t for t in texts), "alidist's recipes give no templates")
g = T.match("git-fetch", "Git command for package 'VecGeom' failed.")
check(fp(g) == ["Git", "'VecGeom'"], f"an alidist package name, and the version control's ({g})")
check(fp(T.match("git-fetch", "Git command for package 'MySecretPkg' failed.")) == ["Git", None],
      "a package alidist does not have: withheld")
for v, want in [("PWGHF/D2H/Tasks/taskD0.cxx", True), ("O2Physics/PWGHF/D2H/Tasks/taskD0.cxx:12:3", True),
                ("./PWGHF/D2H/Tasks/newTask.cxx", True), ("PWGHF/D2H/Tasks", True), ("CCDB/src", True),
                ("MC/bin/o2dpg_sim_workflow.py", True),
                ("PWGHF/D2H/Tasks/../../../secret.txt", False), ("NotATree/x.cxx", False),
                ("hist/hist/src/THnBase.cxx", False)]:
    got = T.public_filler(v)[0] is not None
    check(got == want, f"relative path in the O2 trees {v!r}: {'shown' if got else 'withheld'}")
for v, want in [("/home/meide/alice/O2Physics/PWGHF/D2H/Tasks/taskD0.cxx:12:3",
                 "<path>/O2Physics/PWGHF/D2H/Tasks/taskD0.cxx:12:3"),
                ("/sw/SOURCES/O2/v1/0/CCDB/src/CcdbApi.cxx", "<path>/CCDB/src/CcdbApi.cxx"),
                ("'/build/x/PWGHF/D2H/Tasks'", "<path>/PWGHF/D2H/Tasks"),
                ("/home/meide/private/notes.txt", None), ("/home/meide/CCDB", None),
                ("/home/x/PWGHF/D2H/Tasks/meide.cxx", None)]:
    got = T.public_filler(v)[0]
    check(got == want, f"an absolute path cut to its public tail: {v!r} -> {got!r}")
w = T.match("error", "[ERROR] Exception while running: <time>[FATAL] Fatal error cfgUseMl. Rethrowing.")
check(fp(w) == ["Fatal error cfgUseMl"], f"a whole log line as a value: its time and severity prefix out ({w})")
check(fp(T.match("error", "[ERROR] Aborting because of missing option riedel")) is None,
      "a name in the code that is also a person's: withheld")
check(fp(T.match("error", "[ERROR] Aborting because of missing option Kučera")) is None, "a letter outside ASCII: withheld")
check(fp(T.match("error", "[ERROR] Aborting because of missing option secretThing")) is None,
      "a name not in the code: withheld")
check(fp(T.match("error", "[ERROR] Cannot open AO2D_1.root owned by user eide")) is None,
      "a value after 'user': withheld")
m = T.match("error", "[FATAL] Got nullptr from CCDB for path Users/m/meide/Tests/A and timestamp 1500")
check(fp(m) == [None, "1500"] and "meide" not in str(m), f"a user's CCDB path: withheld, the timestamp shown ({m})")
m = T.match("error", "[FATAL] Got nullptr from CCDB for path Analysis/PID/TPC/Response and timestamp 1500")
check(fp(m) == ["Analysis/PID/TPC/Response", "1500"], f"a CCDB path of the code ({m})")
m = T.match("error", "[ERROR] Exception while running: [ERROR] Requested resource does not exist: "
                     "http://alice-ccdb.cern.ch/Analysis/PID/TPC/Response/1712345678901. Rethrowing.")
check(m and m["fingerprint"] == "Requested resource does not exist: _"
      and fp(m) == ["http://alice-ccdb.cern.ch/Analysis/PID/TPC/Response/1712345678901"],
      f"a wrapped message with its own severity prefix, and a CCDB URL of the code ({m})")
m = T.match("error", "[FATAL] Got nullptr from CCDB for path "
                     "alien:///alice/data/2023/LHC23zzh/544122/apass4/0310/AO2D.root and timestamp 1")
check(fp(m)[0].startswith("alien:///alice/data/2023/"), "a production dataset path")
m = T.match("error", "[FATAL] Got nullptr from CCDB for path alien:///alice/cern.ch/user/a/aalkin/x.root and timestamp 1")
check(fp(m)[0] is None, "a grid user's path: withheld")

shutil.rmtree(work)
print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
