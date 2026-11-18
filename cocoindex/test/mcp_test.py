"""End-to-end test of mcp_server.py against a published snapshot store.

    python test/mcp_test.py <file:///.../bucket/cocoindex>

Starts fake_embedder.py, then drives the server twice through the real MCP
client: over stdio, and over Streamable HTTP with a token file. Asserts on
results, not just "no exception". Needs mcp, sqlite-vec, numpy, httpx.
"""
import asyncio
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "mcp_server.py"
sys.path.insert(0, str(HERE.parent))
import embedtext  # noqa: E402
sys.path.insert(0, str(HERE))
import fake_otlp  # noqa: E402
STORE = sys.argv[1]
failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def payload(res):
    """Structured result of a tool call, or the error text."""
    if res.is_error:
        return {"__error__": " ".join(getattr(c, "text", "") for c in res.content)}
    if res.structured_content is not None:
        sc = res.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(res.content[0].text)


async def wait_ready(client):
    for _ in range(100):
        r = payload(await client.call_tool("index_status", {}))
        if "__error__" not in r:
            return r
        await asyncio.sleep(0.2)
    raise SystemExit("server never served a snapshot: " + r["__error__"])


async def exercise(client, label, cache):
    st = await wait_ready(client)
    check(st["repos"] and st["repos"][0]["commits"] > 0, f"[{label}] index_status lists commits: {st['repos']}")
    tools = {t.name for t in (await client.list_tools()).tools}
    check(tools == {"search_commits", "search_diffs", "find_commits", "similar_commits",
                    "get_commit", "sql", "index_status", "search_prs", "get_pr",
                    "search_ci_failures", "ci_signature", "ci_pr_builds"}, f"[{label}] tool list {sorted(tools)}")

    # Ground truth straight from the cached DB the server downloaded.
    db = sqlite3.connect(f"file:{cache}/commits.db?mode=ro", uri=True)
    repo, sha, subject, body, pr = db.execute(
        "SELECT repo, sha, subject, body, pr_number FROM commits WHERE body != '' "
        "ORDER BY commit_date DESC LIMIT 1").fetchone()
    short = repo.split("/")[1]

    # The fake embedder is a hash of the text, so the flow's exact text must come back at distance 0.
    hits = payload(await client.call_tool("search_commits", {"query": f"{repo}: {subject}\n\n{body}", "k": 3}))
    check(hits[0]["sha"] == sha and hits[0]["distance"] < 1e-4,
          f"[{label}] search_commits finds the exact text first (d={hits[0]['distance']})")
    hits = payload(await client.call_tool("search_commits", {"query": "anything", "repo": short,
                                                             "since": "2026-01-01", "k": 500}))
    check(len(hits) <= 50 and all(h["repo"] == repo and h["date"] >= "2026-01-01" for h in hits),
          f"[{label}] search_commits honours repo short name, since, and caps k ({len(hits)})")
    bad = payload(await client.call_tool("search_commits", {"query": "x", "repo": "NoSuchRepo"}))
    check("unknown repo" in bad.get("__error__", "") if isinstance(bad, dict) else False,
          f"[{label}] unknown repo is an error naming the indexed ones")

    d = payload(await client.call_tool("search_diffs", {"query": "fix", "k": 3, "chunk_chars": 120}))
    check(len(d) == 3 and all(len(h["text"]) <= 120 and h["subject"] for h in d),
          f"[{label}] search_diffs returns truncated chunks with subjects")

    fc = payload(await client.call_tool("find_commits", {"text": subject[:20], "repo": short}))
    check(any(h["sha"] == sha for h in fc), f"[{label}] find_commits text= finds it")
    path = db.execute("SELECT path FROM commit_files WHERE repo=? AND sha=? LIMIT 1", (repo, sha)).fetchone()[0]
    fc = payload(await client.call_tool("find_commits", {"path": path, "k": 50}))
    want = {r[0] for r in db.execute("SELECT sha FROM commit_files WHERE path LIKE ? || '%'", (path,))}
    check({h["sha"] for h in fc} == set(list(want)[:50]) or {h["sha"] for h in fc} <= want and len(fc) == min(50, len(want)),
          f"[{label}] find_commits path= matches commit_files ({len(fc)}/{len(want)})")
    pct = payload(await client.call_tool("find_commits", {"text": "%"}))
    n_pct = db.execute(r"SELECT count(*) FROM commits WHERE subject LIKE '%\%%' ESCAPE '\' OR body LIKE '%\%%' ESCAPE '\'").fetchone()[0]
    check(len(pct) == min(20, n_pct), f"[{label}] find_commits escapes LIKE wildcards ({len(pct)} vs {n_pct})")
    if pr:
        fc = payload(await client.call_tool("find_commits", {"pr": pr}))
        check(any(h["sha"] == sha for h in fc), f"[{label}] find_commits pr= finds it")

    sim = payload(await client.call_tool("similar_commits", {"sha": sha[:10], "k": 5}))
    check(sim["of"]["sha"] == sha and sha not in {h["sha"] for h in sim["similar"]} and len(sim["similar"]) == 5,
          f"[{label}] similar_commits resolves a prefix and excludes itself")

    gc = payload(await client.call_tool("get_commit", {"sha": sha[:8], "diff": True}))
    nfiles = db.execute("SELECT count(*) FROM commit_files WHERE repo=? AND sha=?", (repo, sha)).fetchone()[0]
    check(gc["sha"] == sha and gc["body"] == body and len(gc["files"]) == min(nfiles, 100)
          and "diff_chunks" in gc, f"[{label}] get_commit returns message, {nfiles} files, diff chunks")

    s = payload(await client.call_tool("sql", {"query": "SELECT repo, count(*) n FROM commits GROUP BY repo"}))
    check(s["columns"] == ["repo", "n"] and s["rows"][0][1] == st["repos"][0]["commits"], f"[{label}] sql aggregates")
    s = payload(await client.call_tool("sql", {"query": "SELECT embedding FROM commits LIMIT 2"}))
    check(s["rows"][0][0] == "<768-d vector>", f"[{label}] sql prints vectors as placeholders")
    s = payload(await client.call_tool("sql", {"query": "SELECT sha FROM commits", "max_rows": 5}))
    check(len(s["rows"]) == 5 and s["truncated"], f"[{label}] sql caps rows and says so")
    s = payload(await client.call_tool("sql", {"query":
          "SELECT repo, sha, vec_distance_cosine(embedding, (SELECT embedding FROM commits LIMIT 1)) d "
          "FROM commits ORDER BY d LIMIT 1"}))
    check(s["rows"][0][2] < 1e-4, f"[{label}] sql can call vec_distance_cosine")
    for evil in ["ATTACH DATABASE '/etc/hosts' AS x",
                 f"ATTACH DATABASE '{cache}/commits.db' AS y",
                 "PRAGMA writable_schema=1",
                 "DELETE FROM commits",
                 "CREATE TEMP TABLE t(x)",
                 "SELECT load_extension('/tmp/x')",
                 "SELECT 1; DELETE FROM commits",
                 "WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM r) SELECT count(*) FROM r"]:
        r = payload(await client.call_tool("sql", {"query": evil}))
        check("only SELECT" in r.get("__error__", "") or "exceeded" in r.get("__error__", "") or "one statement" in r.get("__error__", ""), f"[{label}] sql refuses: {evil[:50]} -> {r.get('__error__', r)!s:.70}")
    n = db.execute("SELECT count(*) FROM commits").fetchone()[0]
    s = payload(await client.call_tool("sql", {"query": "SELECT count(*) FROM commits"}))
    check(s["rows"][0][0] == n, f"[{label}] database intact after the attempts ({n} rows)")
    if (Path(cache) / "ci-failures.db").exists():
        await exercise_cf(client, label, cache, st)
    if (Path(cache) / "prs.db").exists():
        await exercise_prs(client, label, cache, st)
    else:
        check(st.get("prs") is None, f"[{label}] index_status: no prs section without prs.db")
        r = payload(await client.call_tool("search_prs", {"query": "x"}))
        check("not in this snapshot" in r.get("__error__", ""), f"[{label}] search_prs without prs.db: clear error")


async def exercise_cf(client, label, cache, st):
    """The CI failure tools, on the store test/cif_test.py leaves (ground truth from the cached db)."""
    L = f"[{label}/ci]"
    cdb = sqlite3.connect(f"file:{cache}/ci-failures.db?mode=ro", uri=True)
    cdb.row_factory = sqlite3.Row
    # The published copy has no error text; the indexer's own db (left by
    # cif_test next to the store) has, for ground truth and for leak checks.
    rdb = sqlite3.connect(f"file:{Path(STORE.removeprefix('file://')).parent.parent}"
                          "/hostA/flows/ci-failures/ci-failures.db?mode=ro", uri=True)
    rdb.row_factory = sqlite3.Row
    cols = [r[1] for r in cdb.execute("PRAGMA table_info(ci_errors)")]
    check("sig_exact" not in cols and "sig_class" not in cols and "fingerprint" in cols,
          f"{L} the served index has hashes and fingerprints, no signature text")
    published = {r[0] for r in cdb.execute("SELECT fingerprint FROM ci_errors WHERE fingerprint IS NOT NULL")}
    hidden = [r[0] for r in rdb.execute("SELECT DISTINCT sig_exact FROM ci_errors WHERE kind != 'crash'")
              if len(r[0]) > 25 and r[0] not in published]
    seen: list[str] = []      # every CI tool reply, checked for hidden text at the end
    idx = {r[0] for r in cdb.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")}
    check({"ci_errors_sig", "ci_errors_class", "ci_builds_pr", "ci_builds_sha"} <= idx,
          f"{L} reader added the CI indexes to its copy")
    cf = st.get("ci_failures") or {}
    n_builds = cdb.execute("SELECT count(*) FROM ci_builds").fetchone()[0]
    check(sum(r["builds"] for r in cf.get("repos", [])) == n_builds and cf.get("signatures_embedded"),
          f"{L} index_status covers CI builds ({n_builds}) and signatures")

    def no_text_outside(obj, texts):
        dump = json.dumps({k: v for k, v in obj.items() if k != "untrusted"})
        return not any(t and t in dump for t in texts)

    # search: the flow's exact embedded text comes back at distance 0
    s = rdb.execute("SELECT sig_hash, kind, sig_exact FROM ci_signatures WHERE kind = 'missing-system-dep' "
                    "LIMIT 1").fetchone()
    r = payload(await client.call_tool("search_ci_failures", {"query": f"{s['kind']}: {s['sig_exact']}", "k": 3}))
    seen.append(json.dumps(r))
    hit = r["results"][0]
    check(hit["sig_hash"] == s["sig_hash"] and hit["distance"] < 1e-4 and hit["infra"] and hit["prs"] >= 3,
          f"{L} search_ci_failures finds the GLUT signature exactly, infra, on >= 3 PRs ({hit['prs']})")
    check(hit["untrusted"]["fingerprint"] is None and s["sig_exact"] not in json.dumps(hit)
          and "untrusted" in r["note"], f"{L} a CMake error is found by meaning but returned by hash only")
    check(hit["latest"] and hit["latest"].startswith("https://ali-ci.cern.ch/alice-build-logs/") and "#L" in hit["latest"],
          f"{L} search_ci_failures: each hit points at its newest log, at the line ({hit['latest']})")
    r = payload(await client.call_tool("search_ci_failures", {"query": "x", "repo": "O2Physics",
                                                              "check": "build/O2Physics/code", "k": 500}))["results"]
    want = {x[0] for x in cdb.execute("SELECT DISTINCT e.sig_hash FROM ci_errors e JOIN ci_signatures s USING "
                                      "(sig_hash) WHERE e.repo = 'AliceO2Group/O2Physics' "
                                      "AND e.check_name LIKE 'build/O2Physics/code%'")}
    check({h["sig_hash"] for h in r} == want and len(r) <= 50,
          f"{L} search_ci_failures: repo short name and check prefix filter ({len(want)})")
    r = payload(await client.call_tool("search_ci_failures", {"query": "x", "since": "2026-11-01", "k": 50}))["results"]
    check(r and all(h["last_seen"] >= "2026-11-01" for h in r), f"{L} search_ci_failures since=")
    r = payload(await client.call_tool("search_ci_failures", {"query": "x", "repo": "NoSuchRepo"}))
    check("unknown repo" in r.get("__error__", ""), f"{L} unknown repo is an error naming the indexed ones")

    # ci_signature from a raw log line, another PR's copy of it, and a hash
    raw = ("/sw/SOURCES/O2Physics/slc9_x86-64-slc9_x86-64/0/PWGLF/Tasks/Strangeness/lambdaspincorrderived.cxx"
           ":4555:69: error: C-style casts are discouraged; use static_cast [google-readability-casting,"
           "-warnings-as-errors]")
    g = payload(await client.call_tool("ci_signature", {"sig": raw}))
    seen.append(json.dumps(g))
    gold = rdb.execute("SELECT sig_hash, count(*) n FROM ci_errors WHERE sig_exact = 'PWGLF/Tasks/Strangeness/"
                       "lambdaspincorrderived.cxx: error: C-style casts are discouraged; use static_cast "
                       "[google-readability-casting]' GROUP BY sig_hash").fetchone()
    check(g["untrusted"]["fingerprint"] == "error: C-style casts are discouraged; use static_cast "
          "[google-readability-casting]", f"{L} a compiler diagnostic keeps its template ({g['untrusted']})")
    check(g.get("match") == "exact" and g["sig_hash"] == gold["sig_hash"] and g["builds"] == gold["n"]
          and g["in_pr_files"]["yes"] >= 1, f"{L} ci_signature: a raw log line (absolute path, line:col) "
                                            f"matches its signature exactly ({g.get('match')}, {g.get('builds')})")
    g2 = payload(await client.call_tool("ci_signature", {"sig": raw.replace(":4555:69:", ":17:2:").replace(
        "/sw/SOURCES/O2Physics/slc9_x86-64-slc9_x86-64/0/", "/Volumes/build/x/osx_arm64/o2physics/sw/SOURCES/"
                                                               "O2Physics/18001/0/")}))
    check(g2.get("sig_hash") == g["sig_hash"], f"{L} ci_signature: another builder's prefix and line numbers, same error")
    g3 = payload(await client.call_tool("ci_signature", {"sig": raw.replace("Strangeness/lambdaspincorrderived",
                                                                            "Other/newTask")}))
    check(g3.get("match") == "class" and g3["class_hash"] == g["class_hash"] and g3["sig_hash"] is None,
          f"{L} ci_signature: same message in another file matches by class ({g3.get('match')})")
    g4 = payload(await client.call_tool("ci_signature", {"sig": "error: a message no build ever printed, "
                                                                "about C-style casts in a new task"}))
    check(str(g4.get("match", "")).startswith("nearest") and g4.get("sig_hash"),
          f"{L} ci_signature: no hash matches, so the nearest error by meaning, embedded outside the "
          f"query slot ({g4.get('match') or g4})")
    check(no_text_outside(g, [g["untrusted"]["fingerprint"]]) and set(g["untrusted"]) == {"fingerprint"},
          f"{L} ci_signature: only the fingerprint, under 'untrusted'")
    check(g["latest"]["log_url"].startswith("https://ali-ci.cern.ch/alice-build-logs/")
          and isinstance(g["latest"]["line"], int) and g["latest"]["line"] > 0
          and g["latest"]["location"] == f"{g['latest']['log_url']}#L{g['latest']['line']}",
          f"{L} ci_signature: the newest occurrence as a pointer, log_url + line ({g['latest']})")
    crash = cdb.execute("SELECT sig_hash, fingerprint FROM ci_errors WHERE kind = 'crash'").fetchone()
    g = payload(await client.call_tool("ci_signature", {"sig": crash["sig_hash"]}))
    check(g.get("kind") == "crash" and g["builds"] == 1 and g["untrusted"]["fingerprint"] == crash["fingerprint"]
          and crash["fingerprint"].startswith("crash in lambda-task"),
          f"{L} ci_signature: a crash fingerprint has a history like any error ({g.get('kind')})")
    glut = rdb.execute("SELECT sig_hash FROM ci_errors WHERE kind = 'cmake' AND sig_exact LIKE '%GLUT%' LIMIT 1").fetchone()[0]
    g = payload(await client.call_tool("ci_signature", {"sig": glut}))
    seen.append(json.dumps(g))
    want = cdb.execute("SELECT count(DISTINCT pr), min(uploaded_at), max(uploaded_at) FROM ci_errors "
                       "WHERE sig_hash = ?", (glut,)).fetchone()
    check(g["match"] == "exact" and g["prs"] == want[0] == 3 and g["first_seen"] == want[1]
          and g["last_seen"] == want[2] and g["hosts"] and all(h.startswith("alloc-") for h in g["hosts"])
          and g["in_pr_files"] == {"yes": 0, "no": 1} and len(g["prs_list"]) == 3,
          f"{L} ci_signature by hash: 3 PRs, first/last seen, hosts, not in the PRs' files")
    rc = cdb.execute("SELECT sig_hash FROM ci_errors WHERE kind = 'remote-cache' LIMIT 1").fetchone()[0]
    g = payload(await client.call_tool("ci_signature", {"sig": rc}))
    check(g["later_green_on_same_sha"] == {"failed_builds": 1, "went_green": 1, "share": 1.0} and g["infra"],
          f"{L} ci_signature: the red build that was re-run green is counted ({g['later_green_on_same_sha']})")
    r = payload(await client.call_tool("ci_signature", {"sig": "0123456789abcdef"}))
    check("no CI error" in r.get("__error__", ""), f"{L} ci_signature: unknown hash is an error")

    # ci_pr_builds
    p = payload(await client.call_tool("ci_pr_builds", {"repo": "O2Physics", "pr": 18073}))
    seen.append(json.dumps(p))
    want = cdb.execute("SELECT count(*) FROM ci_builds WHERE pr = 18073").fetchone()[0]
    stg = sorted((b["attempt"], b["state"], b["host"]) for b in p["builds"] if b["check"] == "build/O2Physics/staging")
    alloc = lambda h: "alloc-" + hashlib.sha256(h.encode()).hexdigest()[:8]   # as cif_test's logs record them
    check(len(p["builds"]) == want == 3 and stg == [(1, "failure", alloc("ci-2f787c.example.org")),
                                                    (2, "success", alloc("ci-11e07d.example.org"))],
          f"{L} ci_pr_builds: every build, the re-run as attempt 2 on another allocation {stg}")
    names = [h for (h,) in cdb.execute("SELECT host FROM ci_builds UNION SELECT host FROM ci_errors") if h]
    check(names and all(h.startswith("alloc-") for h in names),
          f"{L} published hosts are Nomad allocations, never machine names ({sorted(set(names))[:3]})")
    comp = [b for b in p["builds"] if b["check"] == "build/O2Physics/o2"][0]
    check(comp["top_errors"] and all(set(e["untrusted"]) == {"fingerprint"} for e in comp["top_errors"])
          and comp["log_url"] and any(
        e["in_pr_files"] == 1 for e in comp["top_errors"])
          and all("text" not in e and "signature" not in e and e["line"]
                  and e["location"] == f"{comp['log_url']}#L{e['line']}" for e in comp["top_errors"]),
          f"{L} ci_pr_builds: top errors, in_pr_files, the line in log_url, fingerprint under 'untrusted'")
    p = payload(await client.call_tool("ci_pr_builds", {"repo": "AliceO2Group/AliceO2", "pr": 15895}))
    e = [x for b in p["builds"] for x in b.get("top_errors", []) if x["sig_hash"] == glut]
    check(e and e[0]["other_prs_3d"] == 2 and e[0]["in_pr_files"] == 0,
          f"{L} ci_pr_builds: the GLUT error hit 2 other PRs within 3 days ({e and e[0]['other_prs_3d']})")
    p = payload(await client.call_tool("ci_pr_builds", {"repo": "O2Physics", "pr": 18763}))
    check(len(p["builds"]) == 3 and p["summary"]["failing_checks"] == ["build/O2Physics/code-check"],
          f"{L} ci_pr_builds: green and red builds, failing checks summarised")
    r = payload(await client.call_tool("ci_pr_builds", {"repo": "O2Physics", "pr": 999999}))
    check("no indexed CI build" in r.get("__error__", ""), f"{L} ci_pr_builds: unknown PR is an error")
    # runtime messages: the format string of the public code that printed them
    src = cdb.execute("SELECT sig_hash, fingerprint, fingerprint_src, template_id, filler_hash FROM ci_errors "
                      "WHERE fingerprint_kind = 'source' LIMIT 1").fetchone()
    check(src and src["fingerprint"] == "Requested resource does not exist: _"
          and src["fingerprint_src"].startswith("https://github.com/AliceO2Group/AliceO2/blob/")
          and src["fingerprint_src"].endswith("/CCDB/src/CcdbApi.cxx#L2") and len(src["filler_hash"] or "") == 16,
          f"{L} a runtime message is published as its source template, with a permalink and a filler hash")
    g = payload(await client.call_tool("ci_signature", {"sig": src["template_id"]}))
    seen.append(json.dumps(g))
    check(g.get("match") == "template" and g["untrusted"]["fingerprint"] == src["fingerprint"]
          and g["fingerprint_src"] == src["fingerprint_src"] and g["filler_hash"] is None,
          f"{L} ci_signature by template_id: every occurrence of that message ({g.get('match')})")
    g = payload(await client.call_tool("ci_signature", {"sig": src["filler_hash"]}))
    seen.append(json.dumps(g))
    check(g.get("match") == "template+fillers" and g["filler_hash"] == src["filler_hash"],
          f"{L} ci_signature by filler_hash: that message with the same values ({g.get('match')})")
    t = cdb.execute("SELECT fingerprint FROM ci_errors WHERE fingerprint_kind = 'test'").fetchall()
    check(("Test: CCDB/test/testCcdbApiHeaders.cxx ***Failed",) in [tuple(x) for x in t],
          f"{L} a ctest failure whose test is in the public tree keeps its name ({[x[0] for x in t]})")
    r = payload(await client.call_tool("search_ci_failures", {"query": "x", "k": 50}))
    seen.append(json.dumps(r))
    p = payload(await client.call_tool("ci_pr_builds", {"repo": "AliceO2Group/AliceO2", "pr": 16034,
                                                        "errors_per_build": 10}))
    seen.append(json.dumps(p))
    check(not any("meide" in x for x in seen), f"{L} the CERN user name in the CCDB test paths reaches no reply")
    leaked = [h[:60] for h in hidden if any(h in x for x in seen)]
    check(hidden and not leaked, f"{L} no error text reaches the client, only hashes and fingerprints "
                                 f"({len(hidden)} texts checked, leaked: {leaked[:2]})")

    # no planted secret reaches a tool, sql over cf, and the guards
    r = payload(await client.call_tool("ci_pr_builds", {"repo": "O2Physics", "pr": 18900, "errors_per_build": 10}))
    dump = json.dumps(r)
    check(r["builds"] and "hunter2" not in dump and "ghp_" not in dump and "AKIA" not in dump
          and "glpat-" not in dump and "BEGIN RSA" not in dump,
          f"{L} the planted secrets do not come back, not even redacted: no error text is returned")
    s = payload(await client.call_tool("sql", {"query":
        "SELECT b.repo, count(*) FROM ci_builds b JOIN cf.ci_errors e USING (build_id) GROUP BY b.repo"}))
    check(s["rows"] and sum(x[1] for x in s["rows"]) == cdb.execute("SELECT count(*) FROM ci_errors").fetchone()[0],
          f"{L} sql over cf, qualified and unqualified")
    for evil in ["DELETE FROM cf.ci_builds", "UPDATE ci_errors SET line = 1", "DETACH DATABASE cf"]:
        r = payload(await client.call_tool("sql", {"query": evil}))
        check("only SELECT" in r.get("__error__", ""), f"{L} sql refuses: {evil}")


async def exercise_prs(client, label, cache, st):
    L = f"[{label}/prs]"
    pdb = sqlite3.connect(f"file:{cache}/prs.db?mode=ro", uri=True)
    idx = {r[0] for r in pdb.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")}
    cidx = {r[0] for r in sqlite3.connect(f"file:{cache}/commits.db?mode=ro", uri=True).execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL")}
    check({"prs_created", "prs_author", "prs_state"} <= idx and "commits_pr" in cidx,
          f"{L} reader added the indexes to its copies {sorted(idx | cidx)}")
    ps = st["prs"]
    n_prs = pdb.execute("SELECT count(*) FROM prs").fetchone()[0]
    check(ps and sum(r["indexed"] for r in ps["repos"]) == n_prs and ps["chunks"],
          f"{L} index_status covers prs ({n_prs} PRs, chunks {ps and ps['chunks']})")

    # Exact texts the flow embedded come back at distance 0 (fake embedder = hash).
    repo, num, title, body, state, author = pdb.execute(
        "SELECT repo, number, title, body, state, author FROM prs WHERE body != '' ORDER BY created_at LIMIT 1").fetchone()
    r = payload(await client.call_tool("search_prs", {"query": f"{repo}#{num}: {title}\n\n{body}", "k": 3}))
    hit = r["results"][0]
    check(hit["number"] == num and hit["distance"] < 1e-4 and hit["matched"] == "description",
          f"{L} search_prs finds a description exactly (#{hit['number']}, d={hit['distance']})")
    check(hit["untrusted"]["title"] == title and set(hit) >= {"url", "state", "author"} and "untrusted" in r["note"],
          f"{L} PR text comes back under 'untrusted', with a note saying what it is")
    check("title" not in hit and "excerpt" not in hit, f"{L} no PR text outside 'untrusted'")
    kind, cnum, ctitle, path, text = pdb.execute(
        "SELECT c.kind, c.number, p.title, c.path, c.text FROM pr_chunks c JOIN prs p USING (repo, number) "
        "WHERE c.kind = 'thread' LIMIT 1").fetchone()
    q = f"{repo}#{cnum} {ctitle}\n{kind}{' ' + path if path else ''}: {text}"
    r = payload(await client.call_tool("search_prs", {"query": q, "k": 3}))["results"]
    check(r[0]["number"] == cnum and r[0]["matched"] == "thread" and r[0]["matched_path"] == path
          and r[0]["distance"] < 1e-4, f"{L} search_prs matches a review-thread chunk, with its path")
    r = payload(await client.call_tool("search_prs", {"query": "gain correction", "state": "closed", "k": 50}))["results"]
    want = {x[0] for x in pdb.execute("SELECT number FROM prs WHERE state = 'closed'")}
    check({h["number"] for h in r} == want and all(h["state"] == "closed" for h in r),
          f"{L} search_prs state=closed means closed without merging ({sorted(want)})")
    r = payload(await client.call_tool("search_prs", {"query": "x", "author": author.upper(), "k": 50}))["results"]
    want = {x[0] for x in pdb.execute("SELECT number FROM prs WHERE author = ?", (author,))}
    check({h["number"] for h in r} == want, f"{L} search_prs author= is case-insensitive ({len(want)})")
    r = payload(await client.call_tool("search_prs", {"query": "x", "state": "rejected"}))
    check("state must be" in r.get("__error__", ""), f"{L} bad state is an error naming the valid ones")
    r = payload(await client.call_tool("search_prs", {"query": "x", "repo": "NoSuchRepo"}))
    check("unknown repo" in r.get("__error__", ""), f"{L} unknown repo is an error")

    # get_pr: a merged PR that has commits in the commit index, and discussion
    merged = pdb.execute("SELECT repo, number, body FROM prs WHERE state = 'merged' "
                         "ORDER BY n_comments DESC LIMIT 1").fetchone()
    g = payload(await client.call_tool("get_pr", {"number": merged[1]}))
    db = sqlite3.connect(f"file:{cache}/commits.db?mode=ro", uri=True)
    shas = {x[0] for x in db.execute("SELECT sha FROM commits WHERE repo = ? AND pr_number = ?", merged[:2])}
    check(g["untrusted"]["body"] == merged[2] and "body" not in g and "title" not in g,
          f"{L} get_pr: description under 'untrusted' only")
    check({c["sha"] for c in g["commits"]} == shas and all("untrusted" in c for c in g["commits"]),
          f"{L} get_pr lists the merged commits from the commit index ({len(shas)})")
    nchunks = pdb.execute("SELECT count(*) FROM pr_chunks WHERE repo = ? AND number = ? AND kind != 'diff'",
                          merged[:2]).fetchone()[0]
    check(len(g["discussion"]) == nchunks and all(set(d) >= {"kind", "author", "untrusted"} and "text" not in d
                                                  for d in g["discussion"]),
          f"{L} get_pr: {nchunks} discussion chunks, text only under 'untrusted'")
    g1 = payload(await client.call_tool("get_pr", {"number": merged[1], "max_chunks": 1}))
    check(len(g1["discussion"]) == 1 and g1["discussion_omitted"] == nchunks - 1, f"{L} get_pr caps max_chunks")
    g0 = payload(await client.call_tool("get_pr", {"number": merged[1], "discussion": False}))
    check("discussion" not in g0, f"{L} get_pr discussion=false")
    unmerged = pdb.execute("SELECT number FROM prs WHERE state != 'merged' AND EXISTS (SELECT 1 FROM pr_chunks c "
                           "WHERE c.repo = prs.repo AND c.number = prs.number AND kind = 'diff') LIMIT 1").fetchone()
    if unmerged:
        g = payload(await client.call_tool("get_pr", {"number": unmerged[0]}))
        check(g["diff_files"] and not g["commits"], f"{L} get_pr of an unmerged PR lists its diff files")
    r = payload(await client.call_tool("get_pr", {"number": 999999}))
    check("no indexed PR" in r.get("__error__", ""), f"{L} unknown PR is an error that says why it may be missing")

    # get_commit shows its PR
    sha, prn = db.execute("SELECT sha, pr_number FROM commits WHERE pr_number IN (%s) LIMIT 1" %
                          ",".join(str(x[0]) for x in pdb.execute("SELECT number FROM prs"))).fetchone()
    gc = payload(await client.call_tool("get_commit", {"sha": sha}))
    check(gc.get("pr", {}).get("number") == prn and "title" in gc["pr"]["untrusted"],
          f"{L} get_commit shows the matching PR (#{prn})")

    # sql: the join, unqualified and qualified names, vectors, and the guards
    want = db.execute("SELECT count(*) FROM commits WHERE pr_number IS NOT NULL").fetchone()[0]
    s = payload(await client.call_tool("sql", {"query":
        "SELECT c.sha, p.number, p.state FROM commits c JOIN prs p ON p.repo = c.repo AND p.number = c.pr_number"}))
    n_join = sum(1 for _ in db.execute("SELECT pr_number FROM commits WHERE pr_number IS NOT NULL")
                 if pdb.execute("SELECT 1 FROM prs WHERE number = ?", _).fetchone())
    check(len(s["rows"]) == n_join and n_join > 0, f"{L} sql joins commits to prs on (repo, pr_number) ({n_join}/{want})")
    s = payload(await client.call_tool("sql", {"query": "SELECT kind, count(*), max(embedding) FROM pr.pr_chunks GROUP BY kind"}))
    check(s["rows"] and all(r[2] == "<768-d vector>" for r in s["rows"]), f"{L} sql over pr.pr_chunks, vectors as placeholders")
    for evil in ["DETACH DATABASE pr", "DELETE FROM pr.prs", "UPDATE prs SET title = 'x'",
                 "SELECT * FROM pragma_database_list"]:
        r = payload(await client.call_tool("sql", {"query": evil}))
        check("only SELECT" in r.get("__error__", ""), f"{L} sql refuses: {evil} -> {r.get('__error__', r)!s:.60}")
    s = payload(await client.call_tool("sql", {"query": "SELECT count(*) FROM prs"}))
    check(s["rows"][0][0] == n_prs, f"{L} prs intact after the attempts")


async def main():
    work = Path(tempfile.mkdtemp(prefix="coco-mcp-"))
    otlp_bodies: list = []
    mport = free_port()
    fake_otlp.serve(mport, otlp_bodies)
    eport = free_port()
    emb = subprocess.Popen([sys.executable, str(HERE / "fake_embedder.py"), str(eport), str(work / "embed.log"),
                            str(work / "embed-texts.log")])
    env = dict(os.environ, COCO_STORE=STORE, OPENAI_API_BASE=f"http://127.0.0.1:{eport}/v1",
               OPENAI_API_KEY="fake", SQL_TIMEOUT_S="2", PYTHONUNBUFFERED="1")
    try:
        time.sleep(0.5)
        # ---- stdio, as Claude Code on a Mac would start it
        cache = work / "cache-stdio"
        params = StdioServerParameters(command=sys.executable, args=[str(SERVER)],
                                       env=dict(env, COCO_CACHE=str(cache)))
        async with Client(params) as c:
            check("search_commits" in (c.instructions or ""), "[stdio] server instructions are sent")
            await exercise(c, "stdio", cache)

        # ---- HTTP with a token file, as on Nomad
        cache = work / "cache-http"
        tok_file = work / "tokens.json"
        tok_file.write_text(json.dumps({"alice": hashlib.sha256(b"s3cret").hexdigest()}))
        port = free_port()
        srv = subprocess.Popen([sys.executable, str(SERVER), "--http", "--port", str(port)],
                               env=dict(env, COCO_CACHE=str(cache), MCP_TOKENS_FILE=str(tok_file),
                                        MCP_ALLOWED_HOSTS="ali-ci.cern.ch", METRICS_INTERVAL_S="1",
                                        OTLP_METRICS_URL=f"http://127.0.0.1:{mport}/otlp/v1/metrics",
                                        OTLP_WRITE_TOKEN="gate"),
                               stderr=open(work / "http.log", "w"))
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    if httpx.get(base + "/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
            check(httpx.get(base + "/healthz").json()["run_id"], "[http] /healthz names the run, no token needed")
            init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "0"}}}
            hdr = {"Accept": "application/json, text/event-stream"}
            check(httpx.post(base + "/mcp", json=init, headers=hdr).status_code == 401, "[http] no token -> 401")
            check(httpx.post(base + "/mcp", json=init, headers=dict(hdr, Authorization="Bearer nope")).status_code == 401,
                  "[http] wrong token -> 401")
            r = httpx.post(base + "/mcp", json=init,
                           headers=dict(hdr, Authorization="Bearer s3cret", Host="evil.example"))
            check(r.status_code in (403, 421), f"[http] foreign Host header refused ({r.status_code})")
            r = httpx.post(base + "/mcp", json=init,
                           headers=dict(hdr, Authorization="Bearer s3cret", Host="ali-ci.cern.ch"))
            check(r.status_code == 200, f"[http] allowed Host accepted ({r.status_code})")

            async with httpx.AsyncClient(headers={"Authorization": "Bearer s3cret"}, timeout=60) as hc:
                async with Client(streamable_http_client(base + "/mcp", http_client=hc)) as c:
                    await exercise(c, "http", cache)
            # Revocation takes effect without a restart.
            time.sleep(1.1)
            tok_file.write_text(json.dumps({}))
            check(httpx.post(base + "/mcp", json=init, headers=dict(hdr, Authorization="Bearer s3cret")).status_code == 401,
                  "[http] token revoked by editing the file, no restart")
        finally:
            srv.terminate()
            srv.wait()
        log = (work / "http.log").read_text()
        check("alice POST /mcp" in log, "[http] caller name logged per request")
        pts = fake_otlp.points(otlp_bodies)
        names = {n for n, _, _ in pts}
        calls = {a["tool"]: v for n, a, v in pts if n == "cocoindex_mcp_calls_count" and a["outcome"] == "ok"}
        print(f"   metrics: {len(otlp_bodies)} push(es), {len(pts)} points, {sorted(names)[:6]}...")
        # ci_signature is only exercised on a store with ci-failures.db (cif_test's).
        want = {"search_commits", "get_commit", "sql"} | (
            {"ci_signature"} if (cache / "ci-failures.db").exists() else set())
        check(want <= set(calls)
              and {"cocoindex_mcp_call_seconds_p95", "cocoindex_mcp_slot_wait_seconds_p95",
                   "cocoindex_mcp_embed_seconds_p50", "cocoindex_mcp_snapshot_age_seconds"} <= names,
              "[http] metrics: calls per tool and outcome, latencies, slot waits, embedding time, snapshot age")
        check(any(n == "cocoindex_mcp_calls_count" and a["outcome"] == "bad_request" for n, a, _ in pts),
              "[http] metrics: a caller's mistake counts as bad_request, apart from errors")
        check(max((v for n, _, v in pts if n == "cocoindex_mcp_callers_1h"), default=0) == 1,
              "[http] metrics: distinct callers are counted")
        blob = json.dumps(otlp_bodies)
        check(not any(x in blob for x in ("alice", "s3cret", "anything", "SELECT", "NoSuchRepo")),
              "[http] metrics carry no caller name, token, query or SQL text")
        check("s3cret" not in log, "[http] token never logged")
        n = sum(int(x) for x in (work / "embed.log").read_text().split())
        print(f"embedding texts sent: {n}")
        # Searches go out as queries; only ci_signature's nearest-error lookup
        # compares a text with stored ones as one of them (embedtext.doc).
        sent = [json.loads(x) for x in (work / "embed-texts.log").read_text().splitlines()]
        q = [t for t in sent if t.startswith(embedtext.QUERY_PREFIX)]
        d = [t for t in sent if t.startswith(embedtext.DOC_PREFIX)]
        check(q and len(q) + len(d) == len(sent),
              f"every text the server embeds carries a task prefix ({len(q)} queries, {len(d)} documents, "
              f"{len(sent) - len(q) - len(d)} bare)")
    finally:
        emb.terminate()
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":  # test/sso_test.py imports exercise() from here
    asyncio.run(main())
