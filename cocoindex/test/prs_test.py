"""Offline end-to-end test of the prs flow, through runner.sh and the file:// store.

    <venv>/bin/python test/prs_test.py <workdir>

Builds a local "GitHub": a git origin (testorg/demo, with refs/pull/<n>/head)
that the mirrors clone through GIT_BASE_URL, and fake_github.py serving fixture
PRs. Then runs the runner with FLOWS=commits,prs five times and asserts on
prs.db and on what was sent to GitHub and to the (fake) embedder:

  1  bootstrap cut short by PRS_MAX_PAGES: the scan must stop mid-way and say so
  2  resumes from the saved cursor, through three kinds of rate limiting
  3  a comment edited, one deleted, a first-timer's association changed (their
     PR untouched): rows rebuilt, the first-timer's PR indexed
  4  nothing changed: no embeddings, one GitHub list request
  5  a new host restores the snapshot: fetch cache came with the state, no re-embedding

Leaves the store at <workdir>/bucket/cocoindex for test/mcp_test.py.
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE.parent / "runner.sh"
WORK = Path(sys.argv[1]).resolve()
REPO = "testorg/demo"
TOKEN = "gate-1"
failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what, flush=True)
    if not cond:
        failures.append(what)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def sh(*args, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


# ---------------------------------------------------------------- the git origin
def build_origin():
    src, origin = WORK / "src", WORK / "origins" / "testorg" / "demo"
    src.mkdir(parents=True)
    origin.parent.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "master", str(src))
    sh("git", "init", "-q", "--bare", str(origin))
    sh("git", "-C", str(origin), "config", "uploadpack.allowFilter", "true")
    sh("git", "-C", str(origin), "config", "uploadpack.allowAnySHA1InWant", "true")

    def commit(day, msg, files, branch_from=None):
        if branch_from:
            sh("git", "-C", str(src), "checkout", "-q", "--detach", branch_from)
        for path, text in files.items():
            p = src / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            sh("git", "-C", str(src), "add", path)
        date = f"2026-10-{day:02d}T12:00:00Z"
        env = dict(os.environ, GIT_AUTHOR_NAME="Alice", GIT_AUTHOR_EMAIL="alice@example.org",
                   GIT_COMMITTER_NAME="Alice", GIT_COMMITTER_EMAIL="alice@example.org",
                   GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        sh("git", "-C", str(src), "commit", "-q", "-m", msg, env=env)
        return sh("git", "-C", str(src), "rev-parse", "HEAD")

    commit(1, "Initial import of the demo framework\n\nSets up the CMake project and the core library.",
           {"CMakeLists.txt": "project(demo)\n", "src/core.cxx": "int core() { return 0; }\n"})
    commit(2, "Add feature X for TPC clusters (#1)\n\nImplements feature X: cluster merging across sectors.",
           {"Detectors/TPC/src/Calib.cxx": "void mergeClusters(int sector) {\n  // across sectors\n}\n"})
    commit(8, "Fix crash with empty time frames (#7)\n\nGuard against zero-size inputs in the reader.",
           {"src/reader.cxx": "bool read(int n) { return n > 0; }\n"})
    commit(9, "Speed up track fitting (#8)\n\nCaches the propagator between iterations.",
           {"src/tracking.cxx": "void fit() { /* cached propagator */ }\n"})
    commit(10, "Bump CMake minimum to 3.28 (#9)\n\nNeeded for C++20 modules.",
           {"CMakeLists.txt": "cmake_minimum_required(VERSION 3.28)\nproject(demo)\n"})
    commit(11, "Use 100% of the buffer when flushing\n\nPreviously only 50% of it was used.",
           {"src/buffer.cxx": "void flush() { use(100); }\n"})
    commit(12, "Refactor DPL device setup (#10)\n\nSplits the setup into init and run phases.",
           {"src/device.cxx": "void init(); void run();\n"})
    tip = commit(13, "Document the calibration workflow (#11)\n\nAdds a README section on calibration.",
                 {"README.md": "Demo. Calibraton workflow: run calib.\n"})
    heads = {
        2: commit(14, "Use Kalman smoother in track refit",
                  {"src/tracking.cxx": "void fit() { useKalmanSmoother(); }\n"}, branch_from=tip),
        3: commit(15, "Fix typo in README", {"README.md": "Demo. Calibration workflow: run calib.\n"},
                  branch_from=tip),
        5: commit(16, "Apply gain correction per sector",
                  {"src/calib.cxx": "void calibrate(float gain, int sector) {\n"
                                    "  applyGainCorrection(gain, sector);\n}\n"}, branch_from=tip),
    }
    sh("git", "-C", str(src), "push", "-q", str(origin), f"{tip}:refs/heads/master",
       *(f"{sha}:refs/pull/{n}/head" for n, sha in heads.items()))
    return heads, tip


# ---------------------------------------------------------------- fixtures
def c(login, body, day, typename="User", minimized=False, review_state=None):
    out = {"databaseId": abs(hash((login, body))) % 10**9, "author": {"__typename": typename, "login": login},
           "createdAt": f"2026-10-{day:02d}T09:00:00Z", "lastEditedAt": None, "isMinimized": minimized,
           "body": body}
    if review_state:
        out = {k: out[k] for k in ("databaseId", "author", "createdAt", "body")}
        out.update(state=review_state, submittedAt=out["createdAt"])
    return out


def pr(number, login, assoc, title, body, updated, state="MERGED", head="0" * 40, comments=(),
       reviews=(), threads=(), typename="User"):
    merged = state == "MERGED"
    return {"id": f"PR_{number}", "number": number, "title": title,
            "url": f"https://github.com/{REPO}/pull/{number}",
            "state": state, "merged": merged, "isDraft": False,
            "createdAt": f"2026-10-{number:02d}T08:00:00Z", "updatedAt": updated,
            "closedAt": None if state == "OPEN" else updated, "mergedAt": updated if merged else None,
            "baseRefName": "master", "headRefName": f"feature-{number}", "headRefOid": head,
            "authorAssociation": assoc,
            "author": {"__typename": typename, "login": login} if login else None, "body": body,
            "comments": {"nodes": list(comments)}, "reviews": {"nodes": list(reviews)},
            "reviewThreads": {"nodes": list(threads)}}


def fixtures(heads, edited=False):
    comments1 = [
        c("alice", "Ready for review, tested on the 2025 Pb-Pb sample.", 2),
        c("alibuild", "Error while checking build/O2/o2 for abc123 at 2026-10-02:\n```\nfatal error\n```", 2),
        c("github-actions", "**O2 linter results:** 0 errors <!-- thollander/actions-comment -->", 2, "Bot"),
        c("bob", "Please rename kfoo to kSectorBoundary." if edited else
                 "Please rename the variable kfoo to something descriptive.", 3),
        *([] if edited else [c("carol", "This will break the GPU build on macOS.", 3)]),
        c("spammer", "Buy cheap watches at example.com", 3, minimized=True),
        c("frank", "LGTM after the rename.", 4),
    ]
    p1 = pr(1, "alice", "MEMBER", "Add feature X for TPC clusters",
            "<!-- Please describe your change and link the JIRA ticket. -->\n"
            "Implements feature X: cluster merging across TPC sectors.\n\n\n\n"
            "<!-- Checklist: did you run clang-format? -->",
            "2026-10-20T10:00:00Z" if edited else "2026-10-02T10:00:00Z",
            comments=comments1,
            reviews=[c("bob", "Needs a unit test for the sector boundary.", 3, review_state="CHANGES_REQUESTED"),
                     c("alibuild", "Auto-approving on behalf of @alice.", 4, review_state="APPROVED"),
                     c("alice", "", 4, review_state="COMMENTED")],
            threads=[{"path": "Detectors/TPC/src/Calib.cxx", "line": 42, "originalLine": 40,
                      "isOutdated": False,
                      "comments": {"nodes": [c("bob", "Off-by-one at the sector edge?", 3),
                                             c("alice", "Fixed in the next push.", 3)]}}])
    prs = [
        p1,
        pr(2, "dave", "CONTRIBUTOR", "Use Kalman smoother in track refit",
           "Switches the refit to the Kalman smoother.", "2026-10-05T10:00:00Z", "OPEN", heads[2]),
        pr(3, "newbie", "CONTRIBUTOR" if edited else "FIRST_TIME_CONTRIBUTOR", "Fix typo in README",
           "First PR: fixes a typo in the README.", "2026-10-03T10:00:00Z", "OPEN", heads[3]),
        pr(4, None, "NONE", "Old experiment", "From an account that no longer exists.",
           "2026-10-04T10:00:00Z", "CLOSED"),
        pr(5, "erin", "COLLABORATOR", "Apply gain correction per sector", "Superseded by a later PR.",
           "2026-10-06T10:00:00Z", "CLOSED", heads[5],
           comments=[c("erin", "Closing in favour of the calibration rewrite.", 6)]),
        pr(6, "dependabot", "NONE", "Bump actions/checkout from 4 to 5", "Bumps actions/checkout.",
           "2026-10-07T10:00:00Z", "OPEN", typename="Bot"),
    ]
    for i, (n, title) in enumerate([(7, "Fix crash with empty time frames"), (8, "Speed up track fitting"),
                                    (9, "Bump CMake minimum to 3.28"), (10, "Refactor DPL device setup"),
                                    (11, "Document the calibration workflow")]):
        prs.append(pr(n, "bob" if n % 2 else "alice", "MEMBER", title, f"{title}. See the commit message.",
                      f"2026-10-{8 + i:02d}T10:00:00Z"))
    return {REPO: prs}


def control(port, obj):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/control", data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req).read()


# ---------------------------------------------------------------- driver
def main():
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    heads, _ = build_origin()
    eport, gport = free_port(), free_port()
    elog, glog = WORK / "embed.log", WORK / "github.log"
    elog.touch()
    glog.touch()
    procs = [subprocess.Popen([sys.executable, str(HERE / "fake_embedder.py"), str(eport), str(elog)]),
             subprocess.Popen([sys.executable, str(HERE / "fake_github.py"), str(gport), str(glog), TOKEN])]
    time.sleep(1)
    base_env = dict(os.environ, VENV=str(Path(sys.executable).parent.parent), SKIP_VENV="1", ONCE="1",
                    STORE_URL=f"file://{WORK}/bucket", FLOWS="commits,prs",
                    OPENAI_API_BASE=f"http://127.0.0.1:{eport}/v1", OPENAI_API_KEY="fake",
                    COMMITS_REPOS=f"{REPO}:master", DIFF_EMBED_SINCE="2026-09-01",
                    GIT_BASE_URL=f"file://{WORK}/origins",
                    GITHUB_GRAPHQL_URL=f"http://127.0.0.1:{gport}/graphql", GITHUB_TOKEN=TOKEN,
                    PRS_PAGE_SIZE="4", PRS_NESTED="3")
    texts = lambda: sum(int(x) for x in elog.read_text().split())
    gh_reqs = lambda: [json.loads(line) for line in glog.read_text().splitlines()]

    def run_pass(label, host="hostA", **extra):
        t0, g0 = texts(), len(gh_reqs())
        out = subprocess.run(["bash", str(RUNNER)], env=dict(base_env, COCO_ROOT=str(WORK / host), **extra),
                             capture_output=True, text=True)
        (WORK / f"pass-{label}.log").write_text(out.stdout + out.stderr)
        check(out.returncode == 0 and "prs: done" in out.stderr and "commits: done" in out.stderr,
              f"[{label}] runner and both flows exit 0")
        return out.stderr, texts() - t0, gh_reqs()[g0:]

    def db(host="hostA"):
        return sqlite3.connect(f"file:{WORK / host}/flows/prs/prs.db?mode=ro", uri=True)

    def chunks(conn, number):
        return conn.execute("SELECT kind, author, path, line, text FROM pr_chunks WHERE number = ? "
                            "ORDER BY kind, chunk", (number,)).fetchall()

    try:
        control(gport, {"repos": fixtures(heads)})

        # ---- 1: bootstrap cut short after two pages
        err, n_texts, reqs = run_pass("1", PRS_MAX_PAGES="2")
        check("PRS_MAX_PAGES=2 reached" in err, "[1] the scan stops at the page cap and says so")
        lists = [r for r in reqs if r["op"] == "PRList"]
        check(len(lists) == 2 and lists[0]["variables"]["after"] is None,
              f"[1] two list pages, the first from the top ({len(lists)})")
        more = [r["op"] for r in reqs if r["op"].startswith("PRMore_")]
        conn = db()
        got = {r[0] for r in conn.execute("SELECT number FROM prs")}
        check(got == {2, 5, 7, 8, 9, 10, 11}, f"[1] the 8 newest PRs fetched, 7 indexed, the bot's not ({sorted(got)})")
        rep = conn.execute("SELECT indexed, skipped, bots, scan_in_progress FROM pr_repos").fetchone()
        check(rep == (7, 0, 1, 1), f"[1] pr_repos says the scan is in progress {rep}")
        diff5 = [r for r in chunks(conn, 5) if r[0] == "diff"]
        check(diff5 and diff5[0][2] == "src/calib.cxx" and "applyGainCorrection" in diff5[0][4],
              "[1] closed-unmerged #5: diff of refs/pull/5/head embedded")
        check(any(r[0] == "diff" and "useKalmanSmoother" in r[4] for r in chunks(conn, 2)),
              "[1] open #2: diff embedded")
        check(not any(r[0] == "diff" for n in (7, 8) for r in chunks(conn, n)),
              "[1] merged PRs: no diff (the commits flow has it)")
        ncommits = sqlite3.connect(f"file:{WORK}/hostA/flows/commits/commits.db?mode=ro", uri=True) \
            .execute("SELECT count(*) FROM commits").fetchone()[0]
        check(ncommits == 8, f"[1] the commits flow ran next to it ({ncommits} commits)")
        print(f"     pass 1: {n_texts} texts embedded, {len(reqs)} GitHub requests", flush=True)

        # ---- 2: resume, through rate limits
        control(gport, {"inject": {"http_403": 1, "graphql_rate_limited": 1, "low_remaining": 1}})
        err, n_texts, reqs = run_pass("2")
        lists = [r for r in reqs if r["op"] == "PRList"]
        check(lists and lists[0]["variables"]["after"] is not None,
              "[2] the scan resumes from the saved cursor, not from the top")
        check(sum(r["status"] == 403 for r in reqs) == 1 and sum(r["rate_limited"] for r in reqs) == 1,
              "[2] the fake served one HTTP 403 and one RATE_LIMITED")
        check("HTTP 403 rate limited" in err and "RATE_LIMITED; sleeping" in err and "< reserve" in err,
              "[2] the flow backed off for all three (403, RATE_LIMITED, rateLimit.remaining)")
        conn = db()
        got = {r[0] for r in conn.execute("SELECT number FROM prs")}
        check(got == {1, 2, 5, 7, 8, 9, 10, 11}, f"[2] first-timer #3, ghost #4, bot #6 not indexed ({sorted(got)})")
        rep = conn.execute("SELECT indexed, skipped, bots, scan_in_progress, fetched_through FROM pr_repos").fetchone()
        check(rep == (8, 2, 1, 0, "2026-10-12T10:00:00Z"), f"[2] scan complete, watermark set {rep}")
        assoc = [r for r in reqs if r["op"] == "PRAssoc"]
        check(len(assoc) == 1 and assoc[0]["variables"]["ids"] == ["PR_3"],
              "[2] skipped authors re-checked: newbie only, the ghost is never asked about")
        check(any(r["op"] == "PRMore_comments" for r in reqs),
              "[2] #1's 7 comments overflow PRS_NESTED=3 and are paged in")
        body = conn.execute("SELECT body FROM prs WHERE number = 1").fetchone()[0]
        check("cluster merging" in body and "<!--" not in body and "Checklist" not in body
              and "\n\n\n" not in body, "[2] HTML template comments stripped from the body")
        ch1 = chunks(conn, 1)
        alltext = "\n".join(r[4] for r in ch1)
        for want in ("rename the variable kfoo", "GPU build", "LGTM after the rename", "Pb-Pb sample"):
            check(want in alltext, f"[2] #1 human comment indexed: {want!r}")
        for bad in ("Error while checking", "O2 linter", "Auto-approving", "cheap watches", "thollander"):
            check(bad not in alltext, f"[2] #1 bot / minimized text absent: {bad!r}")
        check(any(r[0] == "review" and "Needs a unit test" in r[4] and "[changes_requested]" in r[4] for r in ch1),
              "[2] review body indexed with its state")
        th = [r for r in ch1 if r[0] == "thread"]
        check(len(th) == 1 and th[0][2] == "Detectors/TPC/src/Calib.cxx" and th[0][3] == 42
              and "Off-by-one" in th[0][4] and "Fixed in the next push" in th[0][4] and th[0][1] == "bob,alice",
              f"[2] review thread: one chunk with path, line and both authors {th[:1]}")
        n1 = conn.execute("SELECT n_comments FROM prs WHERE number = 1").fetchone()[0]
        check(n1 == 7, f"[2] #1 n_comments = 4 comments + 1 review + 2 thread comments, bots excluded ({n1})")
        print(f"     pass 2: {n_texts} texts embedded, {len(reqs)} GitHub requests", flush=True)

        # ---- 3: comment edited + deleted on #1 (updated); newbie now CONTRIBUTOR (#3 untouched)
        control(gport, {"repos": fixtures(heads, edited=True)})
        err, n_texts, reqs = run_pass("3")
        conn = db()
        alltext = "\n".join(r[4] for r in chunks(conn, 1))
        check("kSectorBoundary" in alltext, "[3] edited comment: new text indexed")
        check("rename the variable kfoo" not in alltext, "[3] edited comment: old text gone")
        check("GPU build" not in alltext, "[3] deleted comment: gone")
        got = {r[0] for r in conn.execute("SELECT number FROM prs")}
        check(3 in got and 4 not in got, "[3] #3 indexed after its author's association changed, #4 (ghost) still not")
        check(any(r["op"] == "PRNodes" and "PR_3" in r["variables"]["ids"] for r in reqs),
              "[3] #3 fetched again by id, without being updated")
        a3 = conn.execute("SELECT author_association, updated_at FROM prs WHERE number = 3").fetchone()
        check(a3[0] == "CONTRIBUTOR" and a3[1].startswith("2026-10-03"), f"[3] #3 row {a3}")
        check(any(r[0] == "diff" for r in chunks(conn, 3)), "[3] open #3: its diff embedded too")
        # #1: one comment chunk changed; #3: body + one diff chunk. Unchanged texts are memo hits.
        check(n_texts == 3, f"[3] only changed texts re-embedded ({n_texts}, expected 3)")
        rep = conn.execute("SELECT indexed, skipped FROM pr_repos").fetchone()
        check(rep == (9, 1), f"[3] pr_repos {rep}")

        # ---- 4: nothing changed
        err, n_texts, reqs = run_pass("4")
        check(n_texts == 0, f"[4] no change: no embeddings ({n_texts})")
        check([r["op"] for r in reqs] == ["PRList"],
              f"[4] no change: one list request, nothing to re-check ({[r['op'] for r in reqs]})")
        check("nothing published" in err, "[4] no change: no new snapshot")

        # ---- 5: a new host restores state (with the fetch cache) + db from the snapshot
        err, n_texts, reqs = run_pass("5", host="hostB")
        check("prs: restored state + db" in err, "[5] prs restored from the snapshot")
        check((WORK / "hostB/flows/prs/state/github/fetch.db").exists(), "[5] the fetch cache came with the state")
        check(n_texts == 0, f"[5] restored host: nothing re-embedded ({n_texts})")
        check([r["op"] for r in reqs] == ["PRList"] and reqs[0]["variables"]["after"] is None,
              "[5] restored host: incremental scan, not a new bootstrap")
        a, b = db("hostA"), db("hostB")
        q = "SELECT repo, number, kind, chunk, text FROM pr_chunks ORDER BY 1, 2, 3, 4"
        check(a.execute(q).fetchall() == b.execute(q).fetchall(), "[5] restored host has the same rows")
    finally:
        for p in procs:
            p.terminate()
    print(f"store: file://{WORK}/bucket/cocoindex")
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":  # test/cif_test.py imports build_origin() and fixtures() from here
    main()
