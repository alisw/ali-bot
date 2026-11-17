"""Offline end-to-end test of the ci-failures flow, through runner.sh and the file:// store.

    <venv>/bin/python test/cif_test.py <workdir>

A fake S3 (fake_s3.py) serves a bucket built from REAL logs (trimmed, in
fixtures/ci-logs/) plus synthetic ones; fake_github.py serves commit status
histories, PR changed files, and prs_test.py's PR fixtures; fake_embedder.py
logs every text it embeds. The runner runs FLOWS=commits,prs,ci-failures six
times (so the store it leaves has all three databases, for mcp_test.py):

  1  bootstrap cut by CIF_MAX_LOGS=3: every build gets its verdict from
     pretty.html, only 3 logs are downloaded, ci_repos says the rest is pending
  2  resumes: the rest is extracted; failure kinds, signatures, in_pr_files,
     status history; secrets scrubbed; a crash's stack-trace fingerprint; every
     error's line points at it in its log, and no log text is in the index;
     one embedding per distinct signature
  3  the bucket changes: a red build re-run green on the same sha (key
     overwritten), a log deleted, an etag changed, a new PR with a known error.
     Old rows stay; only changed logs are downloaded; only new signatures embedded
  4  nothing changed: no log downloads, no embeddings, no snapshot
  5  a new host restores the snapshot (the extraction cache comes with the
     state): nothing re-downloaded, nothing re-embedded, same rows
  6  a log extracted by an older logscan.EXTRACT_VERSION: only it is re-read

Leaves the store at <workdir>/bucket/cocoindex for test/mcp_test.py.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import prs_test  # noqa: E402  (build_origin, fixtures, helpers; its main() does not run on import)
sys.path.insert(0, str(HERE.parent))
import embedtext  # noqa: E402
import fake_otlp  # noqa: E402
sys.path.insert(0, str(HERE.parent.parent / "ci-mcp"))
import logscan  # noqa: E402

RUNNER = HERE.parent / "runner.sh"
FIX = HERE / "fixtures" / "ci-logs"
WORK = Path(sys.argv[1]).resolve()
prs_test.WORK = WORK
TOKEN, S3TOKEN = "gate-1", "s3-gate-1"
B = "alice-build-logs"
check, failures = prs_test.check, prs_test.failures

# Real fixtures (key -> verdict): see fixtures/ci-logs/
CODECHECK = "AliceO2Group/O2Physics/18763/d140a9099a712e2a29bfb83ec7e6854398e123b4/build_O2Physics_code-check"
GREEN = "AliceO2Group/O2Physics/18763/d140a9099a712e2a29bfb83ec7e6854398e123b4/build_O2Physics_o2"
COMPILE = "AliceO2Group/O2Physics/18073/c1cab5bdce7a4ef307b4c41fb1d5deac71f07f8a/build_O2Physics_o2"
GLUT1 = "AliceO2Group/AliceO2/15895/3bde6afa67f83f220e3f9e8e4d9bb46d552ec49f/build_O2_gpu-test-slc10-x86"
GLUT2 = "AliceO2Group/AliceO2/15909/e085cf4d1c3413f0f29994dcf73eaf7e89414b8e/build_O2_gpu-test-slc10-x86"
CCDBTEST = "AliceO2Group/AliceO2/16034/b77e634f38debd8b42ef264a35f6639d0546bd82/build_O2_o2_slc9"
NOOUT = "AliceO2Group/AliceO2/15838/29cf2788a8a90fece24e11fdf73a8b1d929a8e69/build_O2_fullCI_slc9"
DISK = "AliceO2Group/O2Physics/17992/a4eb29b6b63777c1a871b1f0e1a1a76466211e09/build_O2Physics_o2_macOS-arm"
# Synthetic
SECRETS = "AliceO2Group/O2Physics/18900/" + "5e" * 20 + "/build_O2Physics_o2"
FLAKY = "AliceO2Group/O2Physics/18073/c1cab5bdce7a4ef307b4c41fb1d5deac71f07f8a/build_O2Physics_staging"
GLUT3 = "AliceO2Group/AliceO2/15950/" + "3c" * 20 + "/build_O2_gpu-test-slc10-x86"
BRANCH_LATEST = "alisw/alidist/O2PDPSuite-daily-20261001-0700/latest/release_O2PDPSuite_daily-slc10-x86"

SECRET_VALUES = ["ghp_" + "A1b2C3d4E5" * 4, "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
                 "glpat-" + "Zx9Yw8Vu7T" * 2, "AKIAIOSFODNN7EXAMPLE", "hunter2-Sup3rSecret",
                 "pa55w0rd-in-url", "Bearer-eyJhbGciOiJSUzI1NiJ9.payload", "MIIEowIBAAKCAQEA7bq98PrivateKeyBody"]


def ts(day: str) -> float:
    return datetime.fromisoformat(day + "T12:00:00+00:00").timestamp()


def put(key_dir: str, log: str | bytes | None = None, pretty: str | bytes | None = None, when: float | None = None):
    d = WORK / "s3" / B / key_dir
    d.mkdir(parents=True, exist_ok=True)
    for name, data in (("fullLog.txt", log), ("pretty.html", pretty)):
        if data is None:
            continue
        p = d / name
        p.write_bytes(data.encode() if isinstance(data, str) else data)
        if when:
            os.utime(p, (when, when))


def fixture(key_dir: str, name: str) -> bytes:
    return (FIX / (key_dir.replace("/", "__") + "__" + name)).read_bytes()


def mkpretty(verdict: str, host: str, when: float, repo: str, pr: str, sha: str) -> str:
    """The head of a pretty.html as report-pr-errors writes it (see the real ones in fixtures/)."""
    t = datetime.fromtimestamp(when, timezone.utc).strftime("%a %-d %b %Y, %H:%M:%S UTC")
    return (f'<!DOCTYPE html>\n<head>\n  <title>Build results</title>\n</head>\n'
            f'<body class="{"succeeded" if verdict == "success" else "failed"}">\n'
            f'  <h1>The build {verdict}</h1>\n  <table>\n'
            f'    <tr><th>Build host</th><td>{host}</td></tr>\n'
            f'    <tr><th>Started at</th><td>{t}</td></tr>\n    <tr><th>Finished at</th><td>{t}</td></tr>\n'
            f'    <tr><th>Built PR</th>\n        <td><a href="https://github.com/{repo}/pull/{pr}">{repo}#{pr}</a></td></tr>\n'
            f'    <tr><th>Nomad allocation</th>\n        <td><a href="https://alinomad.cern.ch/ui/allocations/'
            f'{hashlib.sha256(host.encode()).hexdigest()[:8]}-0000-1111-2222-333344445555">'
            f'Nomad allocation information</a></td></tr>\n  </table>\n')


def mklog(host: str, when: float, repo: str, pr: str, sha: str, sections: list[tuple[str, list[str]]]) -> str:
    stamp = datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%d-%H:%M:%S")
    out = [f"Finished building on {host} at {stamp}",
           f"Built commit {sha} from https://github.com/{repo}/pull/{pr}",
           # One allocation per builder, so a re-run elsewhere has another one.
           "Nomad allocation:", "    https://alinomad.cern.ch/ui/allocations/"
           + hashlib.sha256(host.encode()).hexdigest()[:8] + "-0000-1111-2222-333344445555",
           "The following files (oldest first) are present in the log:"]
    out += [f"- sw/BUILD/{pkg}-latest/log" for pkg, _ in sections]
    for pkg, lines in sections:
        out.append(f"## Begin sw/BUILD/{pkg}-latest/log Mon Nov 16 09:05:43 2026")
        out += lines
        out.append(f"## End sw/BUILD/{pkg}-latest/log Mon Nov 16 09:05:44 2026 (1.0s)")
    return "\n".join(out) + "\n"


def secrets_log(when):
    sha = SECRETS.split("/")[3]
    s = SECRET_VALUES
    return mklog("ci-9619f5", when, "AliceO2Group/O2Physics", "18900", sha, [("O2Physics", [
        "[1/3] Building CXX object PWGLF/Tasks/CMakeFiles/secretTask.cxx.o",
        f"/sw/SOURCES/O2Physics/slc9_x86-64-slc9_x86-64/0/PWGLF/Tasks/secretTask.cxx:12:3: error: "
        f"use of undeclared identifier '{s[0]}'",
        "CMake Error at PWGLF/Tasks/CMakeLists.txt:5 (message):",
        f"  Authorization: {s[6]}",
        f"  password={s[4]} github_pat={s[1]}",
        f"  cloning https://alice:{s[5]}@gitlab.cern.ch/alice/secret.git with {s[2]}",
        f"  aws key {s[3]}",
        "  -----BEGIN RSA PRIVATE KEY-----",
        f"  {s[7]}",
        "-----END RSA PRIVATE KEY-----",
        "ninja: build stopped: subcommand failed."]), ("O2PhysicsTests", [
        # A DPL device dying: its trace becomes a fingerprint, the reason (with a
        # secret in it) stays in the private cache only, scrubbed.
        f"[960:lambda-task]: [01:09:23][FATAL] token {s[0]} rejected by the CCDB",
        "[960:lambda-task]: Executable is /sw/slc9_x86-64/O2Physics/local1/bin/o2-analysis-lf-secret",
        "[960:lambda-task]:     /sw/slc9_x86-64/O2/local1/lib/libO2Framework.so:     ?? ??:0",
        "[960:lambda-task]:     /sw/slc9_x86-64/O2Physics/local1/lib/libO2PhysicsLF.so: "
        "SecretTask::process(o2::soa::Join<o2::aod::Tracks, o2::aod::TracksExtra> const&)",
        "[960:lambda-task]:     /sw/slc9_x86-64/O2/local1/lib/libO2Framework.so: "
        "o2::framework::DataProcessingDevice::doRun(o2::framework::ServiceRegistryRef)",
        "[960:lambda-task]:     /lib64/libc.so.6:     ?? ??:0",
        "[ERROR] Workflow crashed - PID 960 (lambda-task) did not exit correctly however it's not clear why."])])


def flaky_log(when, green):
    sha = FLAKY.split("/")[3]
    lines = ["[1/2] Building CXX object PWGCF/Core/CMakeFiles/x.cxx.o"]
    if green:
        lines += ["[2/2] Linking CXX executable o2-analysis-cf", "100% tests passed, 0 tests failed out of 12"]
    else:
        lines += ["2026-10-20T10:01:02.000+0200 [1:2] [executioncontext.cpp:739] [ERROR] Failed to download "
                  "output blob 4a1af663d2073f48fc5a80eb489846c0c864dbba02e1aebca3f41eab8cdd51ae: 13: Blob absent",
                  "FAILED: PWGCF/Core/CMakeFiles/x.cxx.o", "ninja: build stopped: subcommand failed."]
    return mklog("ci-2f787c.example.org", when, "AliceO2Group/O2Physics", "18073", sha, [("O2Physics", lines)])


def build_bucket():
    for key, day in ((CODECHECK, "2026-11-16"), (GREEN, "2026-11-16"), (COMPILE, "2026-10-12"),
                     (GLUT1, "2026-10-09"), (GLUT2, "2026-10-11"), (CCDBTEST, "2026-10-21"),
                     (NOOUT, "2026-09-24"), (DISK, "2026-10-24")):
        put(key, fixture(key, "fullLog.txt"), fixture(key, "pretty.html"), ts(day))
    w = ts("2026-11-14")
    put(SECRETS, secrets_log(w), mkpretty("failure", "ci-9619f5", w, "AliceO2Group/O2Physics", "18900",
                                          SECRETS.split("/")[3]), w)
    w = ts("2026-10-20")
    put(FLAKY, flaky_log(w, False), mkpretty("failure", "ci-2f787c.example.org", w, "AliceO2Group/O2Physics",
                                             "18073", FLAKY.split("/")[3]), w)
    # A branch build's "latest" copy (skipped) and an html-only directory (ignored).
    put(BRANCH_LATEST, "Finished building on x at 2026-10-01-07:00:00\nNo logs found.\n", None, ts("2026-10-01"))
    (WORK / "s3" / B / CODECHECK / "artifacts").mkdir(exist_ok=True)
    (WORK / "s3" / B / CODECHECK / "artifacts" / "x.log").write_text("artifact, not a build log\n")


def statuses():
    def st(ctx, *states_days):  # newest first, as GitHub returns them
        return [{"context": ctx, "state": s, "created_at": f"{d}T12:00:30Z", "description": f"{s} at {d}"}
                for s, d in states_days]
    return {
        f"AliceO2Group/O2Physics@{CODECHECK.split('/')[3]}":
            st("build/O2Physics/code-check", ("error", "2026-11-16")) + st("build/O2Physics/o2", ("success", "2026-11-16")),
        # As the CI posts them: pending when a build starts, its result, then (while
        # red) the same "error" re-posted while it waits to be rechecked.
        f"AliceO2Group/O2Physics@{FLAKY.split('/')[3]}":
            [{"context": "build/O2Physics/staging", "state": "error", "created_at": f"2026-10-2{d}T0{h}:00:00Z",
              "description": "Rechecking since Wed 0{h}:00 CET on 95cff464"} for d, h in ((1, 3), (1, 2), (0, 9))]
            + st("build/O2Physics/staging", ("error", "2026-10-20"))
            + [{"context": "build/O2Physics/staging", "state": "pending", "created_at": "2026-10-20T11:00:00Z",
                "description": "Started"}]
            + st("build/O2Physics/o2", ("error", "2026-10-12")),
        f"AliceO2Group/AliceO2@{GLUT1.split('/')[3]}": st("build/O2/gpu-test-slc10-x86", ("error", "2026-10-09")),
    }


def statuses_after_rerun():
    s = statuses()
    k = f"AliceO2Group/O2Physics@{FLAKY.split('/')[3]}"
    s[k] = [{"context": "build/O2Physics/staging", "state": "success", "created_at": "2026-10-22T12:00:30Z",
             "description": ""},
            {"context": "build/O2Physics/staging", "state": "pending", "created_at": "2026-10-22T11:00:00Z",
             "description": "Started"}] + s[k]
    return s


def pr_files():
    return {
        f"AliceO2Group/O2Physics#18763": {"head": CODECHECK.split("/")[3],
                                          "files": ["PWGLF/Tasks/Strangeness/lambdaspincorrderived.cxx"]},
        f"AliceO2Group/O2Physics#18073": {"head": COMPILE.split("/")[3],
                                          "files": ["PWGCF/TwoParticleCorrelations/Tasks/lambdaProtonBalanceFunction.cxx"]},
        f"AliceO2Group/AliceO2#15895": {"head": GLUT1.split("/")[3], "files": ["GPU/GPUTracking/Base/GPUParam.cxx"]},
        f"AliceO2Group/AliceO2#16034": {"head": CCDBTEST.split("/")[3], "files": ["CCDB/src/CcdbApi.cxx"]},
        f"AliceO2Group/O2Physics#18900": {"head": SECRETS.split("/")[3], "files": ["PWGLF/Tasks/secretTask.cxx"]},
    }


def build_src_origin():
    """A public "AliceO2" with the statement that prints the fixture's CCDB error
    and the test ctest names, for srctemplates.py (SRC_REPOS)."""
    d = WORK / "origins" / "AliceO2Group" / "AliceO2"
    files = {
        "CCDB/src/CcdbApi.cxx": 'void f() {\n  LOG(error) << "Requested resource does not exist: " << url;\n}\n',
        "CCDB/test/testCcdbApiHeaders.cxx": "int main() { return 0; }\n",
        "CCDB/CMakeLists.txt": "o2_add_test(CcdbApiHeaders SOURCES test/testCcdbApiHeaders.cxx)\n",
        "README.md": "not a source file: not checked out\n",
    }
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    git = lambda *a: subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)
    git("init", "-q", "-b", "dev")
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "sources")


# ---------------------------------------------------------------- driver
def main():
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    heads, _ = prs_test.build_origin()
    build_bucket()
    build_src_origin()
    eport, gport, sport = prs_test.free_port(), prs_test.free_port(), prs_test.free_port()
    elog, tlog, glog, slog = WORK / "embed.log", WORK / "texts.jsonl", WORK / "github.log", WORK / "s3.log"
    for f in (elog, tlog, glog, slog):
        f.touch()
    procs = [subprocess.Popen([sys.executable, str(HERE / "fake_embedder.py"), str(eport), str(elog), str(tlog)]),
             subprocess.Popen([sys.executable, str(HERE / "fake_github.py"), str(gport), str(glog), TOKEN]),
             subprocess.Popen([sys.executable, str(HERE / "fake_s3.py"), str(sport), str(WORK / "s3"), str(slog),
                               S3TOKEN, "7"])]
    otlp_bodies: list = []
    mport = prs_test.free_port()
    fake_otlp.serve(mport, otlp_bodies)
    time.sleep(1)
    base_env = dict(os.environ, OTLP_METRICS_URL=f"http://127.0.0.1:{mport}/otlp/v1/metrics",
                    OTLP_WRITE_TOKEN="gate", VENV=str(Path(sys.executable).parent.parent), SKIP_VENV="1", ONCE="1",
                    STORE_URL=f"file://{WORK}/bucket", FLOWS="commits,prs,ci-failures",
                    OPENAI_API_BASE=f"http://127.0.0.1:{eport}/v1", OPENAI_API_KEY="fake",
                    COMMITS_REPOS=f"{prs_test.REPO}:master", DIFF_EMBED_SINCE="2026-09-01",
                    GIT_BASE_URL=f"file://{WORK}/origins",
                    GITHUB_GRAPHQL_URL=f"http://127.0.0.1:{gport}/graphql", GITHUB_TOKEN=TOKEN,
                    S3_ENDPOINT_URL=f"http://127.0.0.1:{sport}", AWS_ACCESS_KEY_ID=S3TOKEN,
                    PRS_PAGE_SIZE="4", PRS_NESTED="3", CIF_FETCH_THREADS="2",
                    SRC_REPOS="AliceO2Group/AliceO2:dev", CIF_FILLER_KEY="cif-test-filler-key")
    texts = lambda: [json.loads(x) for x in tlog.read_text().splitlines()]
    s3reqs = lambda: [json.loads(x) for x in slog.read_text().splitlines()]
    ghreqs = lambda: [json.loads(x) for x in glog.read_text().splitlines()]
    # commits/prs texts start with the repo; "hello" is the embedder's dimension probe
    # Every text a flow embeds is a document (embedtext.doc); classify by what follows.
    bare = lambda t: t.removeprefix(embedtext.DOC_PREFIX)
    cif_texts = lambda tt: [bare(t) for t in tt if not bare(t).startswith(prs_test.REPO) and t != "hello"]

    def run_pass(label, host="hostA", **extra):
        t0, s0, g0 = len(texts()), len(s3reqs()), len(ghreqs())
        out = subprocess.run(["bash", str(RUNNER)], env=dict(base_env, COCO_ROOT=str(WORK / host), **extra),
                             capture_output=True, text=True)
        (WORK / f"pass-{label}.log").write_text(out.stdout + out.stderr)
        check(out.returncode == 0 and "ci-failures: done" in out.stderr and "prs: done" in out.stderr,
              f"[{label}] runner and the three flows exit 0")
        return out.stderr, texts()[t0:], s3reqs()[s0:], ghreqs()[g0:]

    def db(host="hostA"):
        c = sqlite3.connect(f"file:{WORK / host}/flows/ci-failures/ci-failures.db?mode=ro", uri=True)
        c.row_factory = sqlite3.Row
        return c

    def build(conn, key, attempt=None):
        q = "SELECT * FROM ci_builds WHERE log_key = ?" + (" AND attempt = ?" if attempt else
                                                           " ORDER BY attempt DESC LIMIT 1")
        r = conn.execute(q, (key + "/fullLog.txt", *([attempt] if attempt else []))).fetchone()
        return dict(r) if r else None

    def errors(conn, bid):
        return [dict(r) for r in conn.execute("SELECT * FROM ci_errors WHERE build_id = ? ORDER BY rank", (bid,))]

    n_failed_keys = 9  # failed builds among the 11 current fullLog keys (GREEN and the latest copy excluded)
    try:
        prs_test.control(gport, {"repos": prs_test.fixtures(heads), "statuses": statuses(), "files": pr_files()})

        # ---- 1: bootstrap, 3 logs per pass
        err, tt, s3r, gh = run_pass("1", CIF_MAX_LOGS="3")
        pts = fake_otlp.points(otlp_bodies)
        names = {n for n, _, _ in pts}
        flows = {a["flow"] for n, a, _ in pts if n == "cocoindex_flow_seconds"}
        texts_by_flow = {}
        for n, a, v in pts:
            if n == "cocoindex_embed_texts_count":
                texts_by_flow[a["flow"]] = texts_by_flow.get(a["flow"], 0) + v
        check(flows == {"commits", "prs", "ci-failures"} and {"cocoindex_flow_timed_out", "cocoindex_pass_seconds"} <= names,
              f"[1] metrics: each flow's duration and timeout, and the pass's ({sorted(flows)})")
        check(set(texts_by_flow) == {"commits", "prs", "ci-failures"} and all(texts_by_flow.values())
              and "cocoindex_embed_request_seconds_p95" in names,
              f"[1] metrics: embedding texts and request latency per flow ({texts_by_flow})")
        check({"cocoindex_cif_new_builds", "cocoindex_cif_logs_pending", "cocoindex_cif_signatures",
               "cocoindex_snapshot_published_timestamp", "cocoindex_snapshot_db_bytes"} <= names
              and any(n == "cocoindex_snapshot_signatures" and a.get("fingerprint") == "hash only" for n, a, _ in pts),
              "[1] metrics: the sync's counts, the publish, and signatures by fingerprint kind")
        attrs = {k for _, a, _ in pts for k in a}
        check(attrs <= {"flow", "route", "position", "fingerprint"},
              f"[1] metrics carry only flows, routes and kinds as attributes ({sorted(attrs)})")
        conn = db()
        n_builds = conn.execute("SELECT count(*) FROM ci_builds").fetchone()[0]
        check(n_builds == 10, f"[1] one build per fullLog.txt key, the 'latest' copy skipped ({n_builds})")
        check(sum(r["op"] == "list" for r in s3r) >= 3, "[1] the listing pages through several ListObjectsV2 pages")
        gets = [r for r in s3r if r["op"] == "get"]
        check(len(gets) == 3 and all(r["key"].endswith("fullLog.txt") for r in gets),
              f"[1] CIF_MAX_LOGS=3: exactly 3 logs downloaded ({len(gets)})")
        ranges = [r for r in s3r if r["op"] == "range"]
        check(len(ranges) == 10 and all(r["key"].endswith("pretty.html") and r["bytes"] <= 4096 for r in ranges),
              f"[1] one ranged GET (<= 4 KB) of pretty.html per build ({len(ranges)})")
        got = {r[0] for r in conn.execute("SELECT log_key FROM ci_builds WHERE extracted")}
        check(all(not k.startswith(GREEN) for k in got), "[1] failed builds are downloaded before green ones")
        st = dict(conn.execute("SELECT state, count(*) FROM ci_builds GROUP BY state").fetchall())
        check(st == {"failure": 9, "success": 1}, f"[1] every build has its verdict from pretty.html already {st}")
        rep = conn.execute("SELECT sum(pending), sum(builds), sum(extracted) FROM ci_repos").fetchone()
        check(tuple(rep) == (7, 10, 3), f"[1] ci_repos shows the bootstrap in progress {tuple(rep)}")
        check("left for later passes" in err, "[1] the flow says logs are left for later passes")
        src = WORK / "hostA/sources/AliceO2Group__AliceO2"
        check("srctemplates: 1 templates" in err and (src / "CCDB/src/CcdbApi.cxx").exists()
              and not (src / "README.md").exists(),
              "[1] the template sources are checked out sparse (C++ and CMake only) and indexed")
        print(f"     pass 1: {len(cif_texts(tt))} ci texts embedded, {len(s3r)} S3 requests", flush=True)

        # ---- 2: resume
        err, tt, s3r, gh = run_pass("2")
        conn = db()
        gets = [r for r in s3r if r["op"] == "get"]
        check(len(gets) == 7, f"[2] the other 7 logs downloaded, none again ({len(gets)})")
        check(conn.execute("SELECT count(*) FROM ci_builds WHERE NOT extracted").fetchone()[0] == 0,
              "[2] all builds extracted")
        b = build(conn, CODECHECK)
        e = errors(conn, b["build_id"])
        check(b["check_name"] == "build/O2Physics/code-check" and b["host"] == "ci-94822a" and b["state"] == "failure"
              and b["state_source"] == "pretty" and b["started_at"] == "2026-11-16T08:23:15Z",
              f"[2] code-check build: context from GitHub, host and UTC times from pretty.html "
              f"({b['check_name']}, {b['host']}, {b['started_at']})")
        cast = [x for x in e if "C-style casts are discouraged; use static_cast [google-readability-casting]" in x["sig_exact"]]
        check(cast and cast[0]["file"] == "PWGLF/Tasks/Strangeness/lambdaspincorrderived.cxx"
              and "/sw/SOURCES" not in cast[0]["sig_exact"] and ":531:" not in cast[0]["sig_exact"]
              and cast[0]["in_pr_files"] == 1, f"[2] code-check: path relative, line:col stripped, in the PR's files")
        check(any(x["sig_class"] == "error: C-style casts are discouraged; use static_cast [google-readability-casting]"
                  for x in e), "[2] sig_class is the message template")
        # The same cast error reported with and without the /sw/SOURCES prefix is one signature.
        n_cast_lines = sum(1 for x in e if x["sig_exact"].endswith("static_cast [google-readability-casting]")
                           and x["file"] == "PWGLF/Tasks/Strangeness/lambdaspincorrderived.cxx")
        check(n_cast_lines == 1, f"[2] prefixed and relative forms of one error share a signature ({n_cast_lines})")
        check(b["errors_in_pr_files"] >= 1 and b["errors_with_file"] >= b["errors_in_pr_files"],
              f"[2] build counts errors in the PR's files ({b['errors_in_pr_files']}/{b['errors_with_file']})")

        b = build(conn, COMPILE)
        e = errors(conn, b["build_id"])
        check(b["failed_package"] == "O2Physics" and e[0]["fatal"] and any(
            x["file"] == "PWGCF/TwoParticleCorrelations/Tasks/lambdaProtonBalanceFunction.cxx" and x["in_pr_files"] == 1
            for x in e), "[2] compile error: failed package, file in the PR")

        g1, g2 = build(conn, GLUT1), build(conn, GLUT2)
        k1 = {x["kind"] for x in errors(conn, g1["build_id"])}
        check("missing-system-dep" in k1 and "cmake" in k1 and g1["infra_kinds"] == "missing-system-dep",
              f"[2] GLUT: a missing system dependency is an infra kind ({sorted(k1)})")
        ge = [x for x in errors(conn, g1["build_id"]) if x["kind"] == "cmake"]
        check(ge and ge[0]["in_pr_files"] == 0, "[2] GLUT: the CMake module that failed is not in the PR's files")
        sig = ge[0]["sig_hash"]
        prs = {r[0] for r in conn.execute("SELECT DISTINCT pr FROM ci_errors WHERE sig_hash = ?", (sig,))}
        check(prs == {15895, 15909}, f"[2] the same GLUT error on two PRs shares one signature ({prs})")
        check(g2["errors_in_pr_files"] is None,
              "[2] PR 15909 has no file list from GitHub: in_pr_files unknown (NULL), not 0")

        b = build(conn, NOOUT)
        e = errors(conn, b["build_id"])
        check(b["no_output"] and b["infra_kinds"] == "no-output" and e[0]["kind"] == "no-output",
              "[2] 'No logs found' (551 bytes) is the no-output infra kind")
        b = build(conn, DISK)
        check("disk-full" in (b["infra_kinds"] or ""), f"[2] macOS log: disk-full ({b['infra_kinds']})")
        b = build(conn, CCDBTEST)
        e = errors(conn, b["build_id"])
        check(any(x["kind"] == "test" and x["in_pr_files"] in (0, None) for x in e) and b["host"] == "ci-ec7976",
              "[2] unit test failure in CCDB tests, not among the PR's files")
        b = build(conn, GREEN)
        check(b["state"] == "success" and b["extracted"] and b["n_errors"] is not None,
              "[2] green build extracted too (its errors count as 'seen in green')")

        # secrets
        files = [WORK / "hostA/flows/ci-failures/ci-failures.db",
                 WORK / "hostA/flows/ci-failures/state/s3/logs.db"]
        blob = b"".join(f.read_bytes() for f in files) + "".join(tt).encode()
        leaked = [s for s in SECRET_VALUES if s.encode() in blob]
        check(not leaked, f"[2] no planted secret in ci-failures.db, the cache or the embedded texts ({leaked})")
        b = build(conn, SECRETS)
        e = errors(conn, b["build_id"])
        cache = sqlite3.connect(f"file:{files[1]}?mode=ro", uri=True)
        cached = cache.execute("SELECT errors FROM build WHERE key = ?", (SECRETS + "/fullLog.txt",)).fetchone()[0]
        cache.close()
        check("<redacted>" in cached, "[2] secrets were replaced by <redacted> in the private cache, the errors kept")

        # the index holds signatures and pointers into the log, never log text
        cols = [r[1] for r in conn.execute("PRAGMA table_info(ci_errors)")]
        check("text" not in cols and "line" in cols, f"[2] ci_errors has a line and no text column ({cols})")
        crash = [x for x in e if x["kind"] == "crash"]
        check(len(crash) == 1 and crash[0]["sig_exact"] == "crash in lambda-task (o2-analysis-lf-secret): "
              "SecretTask::process" and crash[0]["fatal"] and crash[0]["line"],
              f"[2] a DPL crash is indexed as its stack-trace fingerprint ({[x['sig_exact'] for x in crash]})")
        check(conn.execute("SELECT count(*) FROM ci_signatures WHERE sig_hash = ?",
                           (crash and crash[0]["sig_hash"],)).fetchone()[0] == 1,
              "[2] and the fingerprint is embedded like any failure signature")
        bad = []
        for r in conn.execute("SELECT e.kind, e.sig_exact, e.line, b.log_key FROM ci_errors e "
                              "JOIN ci_builds b USING (build_id) WHERE b.current"):
            if r["kind"] == "no-output":     # "No logs found": nothing in the log to point at
                continue
            if r["line"] is None:
                bad.append(("no line", r["log_key"], r["sig_exact"][:60]))
                continue
            if r["kind"] == "crash" or r["kind"] in logscan.SIG_BODY_KINDS:
                continue
            log_lines = (WORK / "s3" / B / r["log_key"]).read_text(errors="replace").splitlines()
            got = logscan.signature(logscan.scrub(log_lines[r["line"] - 1]), r["kind"])["sig_exact"]
            if got != r["sig_exact"]:
                bad.append((r["log_key"], r["line"], r["sig_exact"][:60], got[:60]))
        check(not bad, f"[2] every error's line points at that error in its log ({bad[:3]})")

        # embeddings: one per distinct signature of a non-green build, nothing else
        check(all(t.startswith(embedtext.DOC_PREFIX) for t in texts() if t != "hello"),
              "[2] every text the flows embed carries the document prefix")
        ct = cif_texts(texts())
        nsig = conn.execute("SELECT count(*) FROM ci_signatures").fetchone()[0]
        want = conn.execute("SELECT count(DISTINCT sig_hash) FROM ci_errors WHERE state != 'success'").fetchone()[0]
        check(len(ct) == nsig == want and len(set(ct)) == len(ct),
              f"[2] one embedding per distinct failure signature, none twice ({len(ct)} texts, {nsig} signatures)")
        green_only = conn.execute("""SELECT count(*) FROM (SELECT sig_hash FROM ci_errors GROUP BY sig_hash
                                     HAVING max(state != 'success') = 0)""").fetchone()[0]
        check(conn.execute("SELECT count(*) FROM ci_signatures WHERE sig_hash IN (SELECT sig_hash FROM ci_errors "
                           "GROUP BY sig_hash HAVING max(state != 'success') = 0)").fetchone()[0] == 0,
              f"[2] signatures seen only in green builds are not embedded ({green_only})")
        ops = [r["op"] for r in ghreqs()]  # passes 1 and 2
        check(ops.count("REST:statuses") >= 8 and "CIFFiles" in ops,
              f"[2] GitHub: status history per commit and PR files batched ({ops.count('REST:statuses')}, "
              f"{ops.count('CIFFiles')})")
        fl = build(conn, FLAKY)
        check(fl["state"] == "failure" and fl["gh_runs"] == 1 and fl["infra_kinds"] == "remote-cache",
              f"[2] FLAKY: red, one run so far, remote-cache infra ({fl['gh_runs']}, {fl['infra_kinds']})")
        print(f"     pass 2: {len(cif_texts(tt))} ci texts embedded, {len(s3r)} S3 requests", flush=True)

        # ---- 3: the bucket changes
        n_sig_before = nsig
        w = ts("2026-10-22")
        put(FLAKY, flaky_log(w, True), mkpretty("success", "ci-11e07d.example.org", w, "AliceO2Group/O2Physics",
                                                "18073", FLAKY.split("/")[3]), w)          # re-run, green
        shutil.rmtree(WORK / "s3" / B / CCDBTEST)                                         # expired
        w2 = ts("2026-11-16") + 600
        cc = fixture(CODECHECK, "fullLog.txt").decode() + (
            "PWGLF/Tasks/Strangeness/lambdaspincorrderived.cxx:600:1: error: brand new problem [bugprone-new]\n")
        put(CODECHECK, cc, fixture(CODECHECK, "pretty.html"), w2)                           # etag changed
        w3 = ts("2026-10-10")
        put(GLUT3, fixture(GLUT1, "fullLog.txt").replace(b"3bde6afa67f83f220e3f9e8e4d9bb46d552ec49f",
                                                         GLUT3.split("/")[3].encode()),
            mkpretty("failure", "ci-9d9f7f.example.org", w3, "AliceO2Group/AliceO2", "15950", GLUT3.split("/")[3]), w3)
        prs_test.control(gport, {"statuses": statuses_after_rerun()})
        err, tt, s3r, gh = run_pass("3")
        conn = db()
        gets = sorted(r["key"].split("/")[2] for r in s3r if r["op"] == "get")
        check(gets == ["15950", "18073", "18763"], f"[3] only the 3 new or changed logs downloaded ({gets})")
        a1, a2 = build(conn, FLAKY, 1), build(conn, FLAKY, 2)
        check(a1 and a2 and a1["state"] == "failure" and a2["state"] == "success" and not a1["current"]
              and a2["current"] and a2["host"] == "ci-11e07d.example.org",
              "[3] overwritten key: attempt 1 (red, kept) and attempt 2 (green, current), both rows")
        check(a2["gh_runs"] == 2 and a2["gh_failures"] == 1 and a2["gh_state"] == "success",
              f"[3] the status history shows red then green on the same sha ({a2['gh_runs']}, {a2['gh_failures']})")
        b = build(conn, CCDBTEST)
        check(b and b["gone"] and errors(conn, b["build_id"]), "[3] a log deleted from the bucket keeps its rows")
        c1, c2 = build(conn, CODECHECK, 1), build(conn, CODECHECK, 2)
        check(c1 and c2 and any("brand new problem" in x["sig_exact"] for x in errors(conn, c2["build_id"]))
              and not any("brand new problem" in x["sig_exact"] for x in errors(conn, c1["build_id"])),
              "[3] changed etag: re-extracted as a new attempt, the old one kept")
        prs = {r[0] for r in conn.execute("SELECT DISTINCT pr FROM ci_errors WHERE sig_hash = ?", (sig,))}
        check(prs == {15895, 15909, 15950}, f"[3] the GLUT signature now on three PRs ({sorted(prs)})")
        ct = cif_texts(tt)
        nsig = conn.execute("SELECT count(*) FROM ci_signatures").fetchone()[0]
        check(len(ct) == nsig - n_sig_before and any("brand new problem" in t for t in ct),
              f"[3] only new signatures embedded ({len(ct)} = {nsig} - {n_sig_before})")

        # ---- 4: nothing changed
        err, tt, s3r, gh = run_pass("4")
        check(not [r for r in s3r if r["op"] in ("get", "range")],
              f"[4] no change: no downloads, listing only ({sorted({r['op'] for r in s3r})})")
        check(not tt, f"[4] no change: no embeddings ({len(tt)})")
        check("nothing published" in err, "[4] no change: no new snapshot")
        check("heads unchanged, index kept" in err, "[4] no change: the template index is kept")

        # ---- 5: a new host restores
        err, tt, s3r, gh = run_pass("5", host="hostB")
        check("ci-failures: restored state + db" in err, "[5] ci-failures restored from the snapshot")
        check((WORK / "hostB/flows/ci-failures/state/s3/logs.db").exists(), "[5] the extraction cache came with the state")
        check(not tt, f"[5] restored host: nothing re-embedded ({len(tt)})")
        check(not [r for r in s3r if r["op"] in ("get", "range")], "[5] restored host: no log downloaded again")
        a, b2 = db("hostA"), db("hostB")
        for t, q in (("ci_builds", "SELECT * FROM ci_builds ORDER BY build_id"),
                     ("ci_errors", "SELECT build_id, sig_hash, line, in_pr_files FROM ci_errors ORDER BY 1, 2"),
                     ("ci_signatures", "SELECT sig_hash, embedding FROM ci_signatures ORDER BY 1")):
            check([tuple(r) for r in a.execute(q)] == [tuple(r) for r in b2.execute(q)],
                  f"[5] restored host has the same {t}")

        # ---- 6: a newer logscan.EXTRACT_VERSION re-reads what an older one extracted
        old = WORK / "hostB/flows/ci-failures/state/s3/logs.db"
        c = sqlite3.connect(old)
        c.execute("UPDATE build SET extracted = ? WHERE key = ?", (logscan.EXTRACT_VERSION - 1, COMPILE + "/fullLog.txt"))
        c.commit()
        c.close()
        q = "SELECT build_id, sig_hash, rank, sig_exact, line, count FROM ci_errors ORDER BY 1, 2"
        before = [tuple(r) for r in db("hostB").execute(q)]
        err, tt, s3r, gh = run_pass("6", host="hostB")
        gets = [r["key"] for r in s3r if r["op"] == "get"]
        check(gets == [COMPILE + "/fullLog.txt"], f"[6] only the log read by an older version is downloaded ({gets})")
        check(not tt, f"[6] re-reading it embeds nothing new ({len(tt)})")
        check([tuple(r) for r in db("hostB").execute(q)] == before, "[6] and its rows come back the same")
    finally:
        for p in procs:
            p.terminate()
    print(f"store: file://{WORK}/bucket/cocoindex")
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
