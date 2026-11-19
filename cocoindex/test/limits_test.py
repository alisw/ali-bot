"""Per-caller limits of mcp_server.py (UserLimits): a rate (token bucket) and a
cap on the calls one caller has running.

    python test/limits_test.py <file:///.../bucket/cocoindex>

Unit checks on UserLimits with a fake clock, then an HTTP server with two static
tokens and low limits: the third call of a burst of 2 is refused with "retry
in N s", another caller is not affected, and stdio has no limits at all.
"""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import anyio
import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import mcp_test as T  # noqa: E402  (reads the store from sys.argv[1])
from mcp import Client  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

os.environ.setdefault("COCO_CACHE", tempfile.mkdtemp(prefix="coco-limits-cache-"))
import mcp_server as S  # noqa: E402

check, free_port, payload = T.check, T.free_port, T.payload


# ---- UserLimits on its own
now = [1000.0]
L = S.UserLimits(max_concurrent=1, wait_s=0.2, rate_per_min=60, burst=3, clock=lambda: now[0])
waits = [L.take("alice") for _ in range(4)]
check(waits[:3] == [0, 0, 0] and 0.9 < waits[3] <= 1.0,
      f"rate: a burst of 3 passes, the 4th waits about 1 s at 60 a minute ({waits})")
check(L.take("bob") == 0, "rate: another caller has a bucket of their own")
now[0] += 1.0
check(L.take("alice") == 0 and L.take("alice") > 0, "rate: one call a second refills, no more")
now[0] += 3601
L.take("bob")
L.prune()
check("alice" not in L._buckets and "bob" in L._buckets, "a caller idle for an hour is forgotten, an active one kept")
check(S.UserLimits(rate_per_min=0).take("x") == 0, "MCP_USER_RATE_PER_MIN=0: no rate limit")


async def concurrency():
    L = S.UserLimits(max_concurrent=1, wait_s=0.2, rate_per_min=0)
    held, release = anyio.Event(), anyio.Event()

    async def hold():
        async with L.slot("alice"):
            held.set()
            await release.wait()

    async with anyio.create_task_group() as tg:
        tg.start_soon(hold)
        await held.wait()
        try:
            async with L.slot("alice"):
                pass
            check(False, "cap: a second call of the same caller waits, then is refused")
        except S.UserBusy as e:
            check(e.running == 1, f"cap: a second call of the same caller waits 0.2 s, then is refused ({e.running} running)")
        async with L.slot("bob"):
            check(True, "cap: another caller is not held up")
        release.set()
    async with L.slot("alice"):
        check(True, "cap: once the first call ends, the next one runs")
    U = S.UserLimits(max_concurrent=0, rate_per_min=0)
    async with U.slot("a"), U.slot("a"), U.slot("a"):
        check(True, "MCP_USER_MAX_CONCURRENT=0: no cap")


anyio.run(concurrency)


# ---- over HTTP: two callers, low limits
async def http():
    work = Path(tempfile.mkdtemp(prefix="coco-limits-"))
    eport = free_port()
    emb = subprocess.Popen([sys.executable, str(HERE / "fake_embedder.py"), str(eport), str(work / "embed.log")])
    tok_file = work / "tokens.json"
    tok_file.write_text(json.dumps({"alice": hashlib.sha256(b"a-token").hexdigest(),
                                    "bob": hashlib.sha256(b"b-token").hexdigest()}))
    port = free_port()
    env = dict(os.environ, COCO_STORE=T.STORE, COCO_CACHE=str(work / "cache"), MCP_TOKENS_FILE=str(tok_file),
               OPENAI_API_BASE=f"http://127.0.0.1:{eport}/v1", OPENAI_API_KEY="fake", PYTHONUNBUFFERED="1",
               MCP_USER_RATE_PER_MIN="6", MCP_USER_BURST="2")
    srv = subprocess.Popen([sys.executable, str(T.SERVER), "--http", "--port", str(port)], env=env,
                           stderr=open(work / "server.log", "w"))
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            try:
                if httpx.get(base + "/healthz").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)

        async def calls(token, n):
            out = []
            async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60) as hc:
                async with Client(streamable_http_client(base + "/mcp", http_client=hc)) as c:
                    for _ in range(n):
                        out.append(payload(await c.call_tool("index_status", {})))
            return out

        a = await calls("a-token", 3)
        errs = [x.get("__error__", "") if isinstance(x, dict) else "" for x in a]
        check(not errs[0] and not errs[1] and "rate limit" in errs[2] and "retry in 10 s" in errs[2],
              f"[http] alice: 2 calls pass, the 3rd is refused with the wait, at 6 a minute ({errs[2]!r})")
        b = await calls("b-token", 1)
        check(not (isinstance(b[0], dict) and "__error__" in b[0]), "[http] bob, at the same moment, is not affected")
    finally:
        srv.terminate()
        srv.wait()
        emb.terminate()

    # stdio: one local user, no limits even with the same settings
    from mcp.client.stdio import StdioServerParameters
    params = StdioServerParameters(command=sys.executable, args=[str(T.SERVER)],
                                   env=dict(env, COCO_CACHE=str(work / "cache-stdio")))
    async with Client(params) as c:
        await T.wait_ready(c)
        r = [payload(await c.call_tool("index_status", {})) for _ in range(4)]
    check(not any(isinstance(x, dict) and "__error__" in x for x in r), "[stdio] no per-caller limits")


asyncio.run(http())
print(f"\n{len(T.failures)} failure(s)")
sys.exit(1 if T.failures else 0)
