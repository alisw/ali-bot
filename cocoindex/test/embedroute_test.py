"""embedroute.py: the first embedding route that answers and gives aigw's vectors.

    python test/embedroute_test.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PICK = [sys.executable, str(HERE.parent / "embedroute.py"), "pick", "fast", "aigw"]
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


work = Path(tempfile.mkdtemp(prefix="embedroute-"))
procs = []


delays = {}


def fake(salt=""):
    port = free_port()
    log = work / f"embed-{port}.log"
    procs.append(subprocess.Popen([sys.executable, str(HERE / "fake_embedder.py"), str(port), str(log)],
                                  env=dict(os.environ, FAKE_EMBED_SALT=salt)))
    url = f"http://127.0.0.1:{port}/v1"
    delays[url] = Path(str(log) + ".delay")       # fake_embedder waits while it exists
    return url


def slow(url, seconds):
    if seconds:
        delays[url].write_text(str(seconds))
    else:
        delays[url].unlink(missing_ok=True)


try:
    aigw, same, other = fake(), fake(), fake("another model")
    dead = f"http://127.0.0.1:{free_port()}/v1"
    time.sleep(1)
    # The first qualifying answer wins: aigw a little slower than the fast
    # route, so the checks about compatibility do not depend on a race.
    slow(aigw, 0.4)
    ref = work / "ref.json"
    shipped = work / "shipped-ref.json"        # absent unless a check writes it

    def pick(fast, canonical=aigw, timeout="3"):
        r = subprocess.run(PICK, capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, ROUTE_FAST_URL=fast, ROUTE_AIGW_URL=canonical,
                                    ROUTE_FAST_TOKEN="t", ROUTE_AIGW_TOKEN="t",
                                    EMBED_REF_FILE=str(ref), EMBED_PICK_TIMEOUT_S=timeout,
                                    EMBED_SHIPPED_REF_FILE=str(shipped)))
        return r.stdout.strip(), r.stderr

    got, _ = pick(same)
    check(got == "fast" and ref.exists(), f"a fast route with aigw's vectors is used, the reference is kept ({got})")
    sizes = (work / f"embed-{same.split(':')[2].split('/')[0]}.log").read_text().split()
    check(sizes and sizes[-1] == "2",
          f"the race sends a fresh sentence along, so a caching server cannot answer from its cache ({sizes[-3:]})")
    got, err = pick(other)
    check(got == "aigw" and "vectors differ" in err,
          f"a fast route with other vectors (another model) is refused: aigw ({got})")
    got, err = pick(dead)
    check(got == "aigw" and "no answer" in err, f"a fast route that does not answer: aigw ({got})")
    got, _ = pick(same, canonical=dead)
    check(got == "fast", f"aigw down: the fast route is checked against the kept reference, and used ({got})")
    got, err = pick(other, canonical=dead)
    check(got == "aigw" and "vectors differ" in err,
          f"aigw down: a wrong fast route is still refused against the kept reference ({got})")
    ref.unlink()
    got, err = pick(same, canonical=dead)
    check(got == "aigw" and "cannot be checked" in err,
          f"aigw down and no reference yet: the fast route cannot be checked, not used ({got})")
    # The reference shipped with the code: a fresh process can check the fast
    # route although aigw has never answered it (the MCP server, 2026-11-18).
    shipped.write_text(json.dumps({"model": "embeddinggemma-300m", "text": json.loads(
        subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import embedroute, json; "
                        "print(json.dumps(embedroute.REF_TEXT))" % str(HERE.parent)],
                       capture_output=True, text=True).stdout), "vector": None}))
    import importlib.util
    spec = importlib.util.spec_from_file_location("er", HERE.parent / "embedroute.py")
    er = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(er)
    os.environ.update(ROUTE_AIGW_URL=aigw, ROUTE_AIGW_TOKEN="t")
    d = json.loads(shipped.read_text())
    d["vector"] = er.embed("aigw", er.REF_TEXT, 5)    # what aigw answers for it (the fakes are deterministic)
    shipped.write_text(json.dumps(d))
    got, err = pick(same, canonical=dead)
    check(got == "fast" and not ref.exists(),
          f"aigw down, nothing kept, but a shipped reference: the fast route is checked and used ({got})")
    other_shipped = dict(d, vector=er.embed("aigw", er.REF_TEXT + " x", 5))
    shipped.write_text(json.dumps(other_shipped))
    got, err = pick(same, canonical=dead)
    check(got == "aigw" and "vectors differ" in err,
          f"...and a shipped reference that does not match still refuses it ({got})")
    shipped.unlink()
    ref.write_text(json.dumps({"model": "something-else", "text": "x", "vector": [1.0]}))
    got, _ = pick(same)
    check(got == "fast" and json.loads(ref.read_text())["model"] == "embeddinggemma-300m",
          f"a reference from another model is ignored and replaced ({got})")
    # Response time: the fastest compatible route, whatever the order.
    slow(same, 1.2)
    got, err = pick(same)
    check(got == "aigw" and "using aigw" in err, f"a compatible but slower fast route: aigw answers first and is used ({got})")
    slow(same, 0)
    slow(aigw, 1.2)
    got, _ = pick(same)
    check(got == "fast", f"aigw slower: the fast route ({got})")
    # aigw hanging: the fast route's answer is used at once, not after aigw's timeout.
    slow(aigw, 8)
    t0 = time.monotonic()
    got, err = pick(same, timeout="20")
    took = time.monotonic() - t0
    check(got == "fast" and took < 3,
          f"aigw hanging: the fast route answers and is used without waiting for aigw ({got}, {took:.1f} s)")
    slow(aigw, 0)

    # ---- the MCP server: query embeddings through the fast route, aigw as fallback
    os.environ.update(MCP_EMBED_ROUTES="fast,aigw", MCP_EMBED_FAST_TIMEOUT_S="2", MCP_EMBED_TIMEOUT_S="5",
                      COCO_CACHE=str(work / "cache"), COCO_STORE=f"file://{work}/store",
                      ROUTE_FAST_TOKEN="t", ROUTE_AIGW_TOKEN="t", EMBED_REF_FILE=str(work / "mcp-ref.json"),
                      EMBED_SHIPPED_REF_FILE=str(work / "no-shipped-ref.json"))
    (work / "cache").mkdir()
    sys.path.insert(0, str(HERE.parent))
    import mcp_server as S  # noqa: E402
    import sqlite_vec  # noqa: E402

    used = []
    S.M.count = lambda name, n=1, **a: used.append((name, a.get("route"))) if name.startswith("cocoindex_mcp_embed") else None

    def query(fast, canonical=aigw, text="tpc calibration"):
        os.environ.update(ROUTE_FAST_URL=fast, ROUTE_AIGW_URL=canonical)
        S._embed_cached.cache_clear()
        used.clear()
        try:
            return S._embed_cached(text), list(used)
        except Exception as e:
            return e, list(used)

    import threading

    def reset():
        S._route_verdict.clear()
        S._route_ewma.clear()
        S._route_due.clear()

    def checked(fast, canonical=aigw, routes=("fast", "aigw")):
        """As the background checker would leave it: every route checked once."""
        reset()
        os.environ.update(ROUTE_FAST_URL=fast, ROUTE_AIGW_URL=canonical)
        for r in routes:
            S._check_route(r)

    slow(aigw, 0.3)                               # so the fast route is measurably faster
    checked(same)
    slow(aigw, 0)
    v_fast, u = query(same)
    check(isinstance(v_fast, bytes) and u == [("cocoindex_mcp_embed_route", "fast")],
          f"MCP: a checked fast route answers the query ({u})")
    reset()
    _, u = query(same, text="before any check")
    check(u == [("cocoindex_mcp_embed_route", "aigw")],
          f"MCP: a route not checked yet is not used; aigw answers ({u})")
    checked(other)
    v_other, u = query(other)
    check(u == [("cocoindex_mcp_embed_route", "aigw")] and v_other == v_fast,
          f"MCP: a fast route with other vectors is refused by its check; aigw's vector, the same as the fast one's ({u})")
    slow(aigw, 0.3)
    checked(same)                                 # fast measured faster than aigw
    _, u = query(dead)                            # ...then fast dies
    check(("cocoindex_mcp_embed_fallback", "fast") in u and ("cocoindex_mcp_embed_route", "aigw") in u,
          f"MCP: the fast route failing in use falls back to aigw at once ({u})")
    _, u = query(dead)
    slow(aigw, 0)
    due = S._route_due.get("fast", 0) - time.monotonic()
    check(u == [("cocoindex_mcp_embed_route", "aigw")] and 0 < due <= S.NO_INSTANCE_RECHECK_S,
          f"MCP: and is not tried again until a check passes it, due in {due:.0f} s ({u})")
    checked(same, canonical=dead)
    _, u = query(same, canonical=dead)
    check(("cocoindex_mcp_embed_route", "fast") in u,
          f"MCP: aigw down, the fast route checked against the kept reference answers ({u})")
    # Fastest first, by the checks' timings: no search is spent measuring.
    slow(same, 1.0)
    checked(same)
    seq = [query(same, text=f"q{i}")[1] for i in range(3)]
    slow(same, 0)
    check([u[-1][1] for u in seq] == ["aigw", "aigw", "aigw"],
          f"MCP: a slower fast route, timed by its check, is not used while aigw is faster ({seq})")
    # A check never holds up a search (2026-11-18: a 14 s check of the ALICE
    # server inside the lock every search needed).
    checked(same)
    slow(same, 3)
    th = threading.Thread(target=S._check_route, args=("fast",))
    th.start()
    time.sleep(0.3)                               # the check is now waiting on the slow route
    S._embed_cached.cache_clear()
    used.clear()
    slow(same, 0)                                 # the search itself is answered fast
    t0 = time.monotonic()
    S._embed_cached("during a check")
    took = time.monotonic() - t0
    th.join()
    check(took < 1.5, f"MCP: a search while a slow check runs is not held up ({took:.2f} s, {used})")
    # The checker loop: every route checked at once at start.
    reset()
    os.environ.update(ROUTE_FAST_URL=same, ROUTE_AIGW_URL=aigw)
    S.ROUTE_CHECK_TICK_S = 0.2
    stop = threading.Event()
    threading.Thread(target=S._route_checker, args=(stop,), daemon=True).start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10 and not (S._route_ok("fast") and "aigw" in S._route_ewma):
        time.sleep(0.1)
    stop.set()
    check(S._route_ok("fast") and set(S._route_ewma) == {"fast", "aigw"},
          f"MCP: the checker checks and times every route at start ({time.monotonic() - t0:.1f} s)")

    # ---- a route served by several instances (the job's CPU servers):
    # ROUTE_<R>_URLS_FILE, kept up to date by a Nomad template.
    urls = work / "cpu-urls"
    os.environ.pop("ROUTE_FAST_URL", None)
    os.environ["ROUTE_FAST_URLS_FILE"] = str(urls)
    os.environ["ROUTE_AIGW_URL"] = aigw

    def query_file(text):
        S._embed_cached.cache_clear()
        used.clear()
        try:
            return S._embed_cached(text), list(used)
        except Exception as e:
            return e, list(used)

    reset()
    urls.write_text("")
    S._check_route("fast")
    slow(aigw, 0.3)                               # so the instances are measurably faster
    S._check_route("aigw")
    slow(aigw, 0)
    _, u = query_file("no instance yet")
    due = S._route_due.get("fast", 0) - time.monotonic()
    check(u == [("cocoindex_mcp_embed_route", "aigw")] and not S._route_ok("fast") and 0 < due <= S.NO_INSTANCE_RECHECK_S,
          f"MCP: no instance registered yet: aigw, and the route is checked again in {due:.0f} s, "
          f"not {S.ROUTE_RECHECK_S} ({u})")
    urls.write_text(f"{same}\n{same}\n")
    S._check_route("fast")
    _, u = query_file("instances registered")
    check(u == [("cocoindex_mcp_embed_route", "fast")],
          f"MCP: once instances are listed, they are checked and answer the query ({u})")
    urls.write_text(f"{dead}\n")
    _, u = query_file("instance gone")
    check(("cocoindex_mcp_embed_route_failed", "fast") in u and ("cocoindex_mcp_embed_route", "aigw") in u,
          f"MCP: an instance gone: the query falls back to aigw at once ({u})")
finally:
    for p in procs:
        p.terminate()

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
