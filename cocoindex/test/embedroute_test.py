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
    ref = work / "ref.json"

    def pick(fast, canonical=aigw):
        r = subprocess.run(PICK, capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, ROUTE_FAST_URL=fast, ROUTE_AIGW_URL=canonical,
                                    ROUTE_FAST_TOKEN="t", ROUTE_AIGW_TOKEN="t",
                                    EMBED_REF_FILE=str(ref), EMBED_PICK_TIMEOUT_S="3"))
        return r.stdout.strip(), r.stderr

    got, _ = pick(same)
    check(got == "fast" and ref.exists(), f"a fast route with aigw's vectors is used, the reference is kept ({got})")
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
    slow(aigw, 0)

    # ---- the MCP server: query embeddings through the fast route, aigw as fallback
    os.environ.update(MCP_EMBED_ROUTES="fast,aigw", MCP_EMBED_FAST_TIMEOUT_S="2", MCP_EMBED_TIMEOUT_S="5",
                      COCO_CACHE=str(work / "cache"), COCO_STORE=f"file://{work}/store",
                      ROUTE_FAST_TOKEN="t", ROUTE_AIGW_TOKEN="t", EMBED_REF_FILE=str(work / "mcp-ref.json"))
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

    S._route_verdict.clear()
    S._route_ewma.clear()
    v_fast, u = query(same)
    check(isinstance(v_fast, bytes) and u == [("cocoindex_mcp_embed_route", "fast")],
          f"MCP: a checked fast route answers the query ({u})")
    S._route_verdict.clear()
    S._route_ewma.clear()
    v_other, u = query(other)
    check(u == [("cocoindex_mcp_embed_route", "aigw")] and v_other == v_fast,
          f"MCP: a fast route with other vectors is skipped; aigw's vector, the same as the fast one's ({u})")
    S._route_verdict.clear()
    S._route_ewma.clear()
    slow(aigw, 0.3)                               # fast leads once both are measured
    query(same, text="a")                         # verdict: fast is fine, and measured
    query(same, text="b")                         # aigw measured too: slower
    _, u = query(dead)                            # ...then fast dies
    check(("cocoindex_mcp_embed_fallback", "fast") in u and ("cocoindex_mcp_embed_route", "aigw") in u,
          f"MCP: the fast route failing in use falls back to aigw at once ({u})")
    _, u = query(dead)
    slow(aigw, 0)
    check(u == [("cocoindex_mcp_embed_route", "aigw")],
          f"MCP: and is not tried again until the next check ({u})")
    S._route_verdict.clear()
    S._route_ewma.clear()
    _, u = query(same, canonical=dead)
    check(("cocoindex_mcp_embed_route", "fast") in u,
          f"MCP: aigw down, the fast route checked against the kept reference answers ({u})")
    # Fastest first: a slow fast route is tried once, then aigw leads.
    S._route_verdict.clear()
    S._route_ewma.clear()
    slow(same, 1.0)
    seq = [query(same, text=f"q{i}")[1] for i in range(3)]
    slow(same, 0)
    check([u[-1][1] for u in seq] == ["fast", "aigw", "aigw"],
          f"MCP: routes are tried fastest first; a slower route is measured once, then aigw leads ({seq})")
finally:
    for p in procs:
        p.terminate()

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
