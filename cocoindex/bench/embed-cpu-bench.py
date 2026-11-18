"""How fast does a CPU embed search queries? (cocoindex, 2026-11-18)

Asks one or more llama-server instances (CPU only, embeddinggemma-300M Q8_0)
for query embeddings at several concurrencies, with a short and a long query,
and checks that the vectors are aigw's (cosine to the shipped reference).

    embed-cpu-bench.py REFERENCE_JSON NAME=URL...

Prints one line per (server, query length, concurrency): latency p50/p95 and
queries per second. Standard library only.
"""
import json
import random
import statistics
import sys
import threading
import time
import urllib.request

SHORT = [  # what an agent types into search_commits (~10-20 tokens)
    "TPC cluster merging crash at sector boundary",
    "why does the ITS reconstruction allocate so much memory",
    "CCDB fetch fails with nullptr for path and timestamp",
    "segfault in o2-analysis-lf-strangenessbuilder",
    "ONNXRuntime build failure on macOS arm",
    "who changed the default for the GPU memory scaling",
]
LONG = (  # a pasted error with context (~200 tokens)
    "[ERROR] Exception caught while in Init: Got nullptr from CCDB for path "
    "GLO/Config/GRPMagField and timestamp 1716212345678. The workflow o2-analysis-"
    "event-selection-service failed to initialise. Stack trace: #0 o2::framework::"
    "DataProcessingDevice::Init #1 fair::mq::Device::InitWrapper #2 std::thread::_State_impl "
    "#3 start_thread. Previous messages: [INFO] CCDB: querying http://alice-ccdb.cern.ch "
    "with If-None-Match, [WARN] CCDB: 404 for GLO/Config/GRPMagField, [INFO] "
    "Device internal-dpl-ccdb-backend exited with code 1. Which commit changed the "
    "CCDB path of the magnetic field configuration, and is this error already known "
    "from other pull requests in O2Physics or AliceO2 during the last weeks? "
)
CONCURRENCY = [1, 4, 16]
PER_WORKER = 12


def embed(url, text, timeout=120):
    req = urllib.request.Request(url.rstrip("/") + "/v1/embeddings", method="POST",
                                 data=json.dumps({"model": "embeddinggemma-300m", "input": [text]}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    v = d["data"][0]["embedding"]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v], (d.get("usage") or {}).get("prompt_tokens")


def wait_up(url, deadline_s=600):
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=5) as r:
                if r.status == 200:
                    return time.monotonic() - t0
        except Exception:
            pass
        time.sleep(2)
    raise SystemExit(f"{url}: not up after {deadline_s} s")


def run(url, texts, conc):
    lat, errors = [], []
    lock = threading.Lock()

    def worker(i):
        rnd = random.Random(i)
        for k in range(PER_WORKER):
            # a fresh suffix: no server-side cache can answer
            t = rnd.choice(texts) + f" ({i}.{k}.{rnd.random():.6f})"
            t0 = time.monotonic()
            try:
                embed(url, t)
                with lock:
                    lat.append(time.monotonic() - t0)
            except Exception as e:
                with lock:
                    errors.append(repr(e)[:100])

    t0 = time.monotonic()
    th = [threading.Thread(target=worker, args=(i,)) for i in range(conc)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    wall = time.monotonic() - t0
    lat.sort()
    p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] * 1000 if lat else float("nan")
    return dict(n=len(lat), errors=len(errors), p50=p(0.5), p95=p(0.95),
                mean=statistics.mean(lat) * 1000 if lat else float("nan"), qps=len(lat) / wall,
                first_error=errors[0] if errors else None)


def main(argv):
    ref = json.load(open(argv[0]))
    servers = [a.split("=", 1) for a in argv[1:]]
    for name, url in servers:
        print(f"{name}: up after {wait_up(url):.0f} s", flush=True)
        v, _ = embed(url, ref["text"])
        cos = sum(x * y for x, y in zip(v, ref["vector"]))
        print(f"{name}: cosine to aigw's reference vector {cos:.5f} (guard: >= 0.999)", flush=True)
        for label, texts in (("short", SHORT), ("long", [LONG])):
            _, toks = embed(url, texts[0])
            for _ in range(3):                      # warm up
                embed(url, texts[0])
            for c in CONCURRENCY:
                r = run(url, texts, c)
                print(f"RESULT server={name} query={label} tokens={toks} concurrency={c} n={r['n']} "
                      f"errors={r['errors']} p50_ms={r['p50']:.0f} p95_ms={r['p95']:.0f} "
                      f"mean_ms={r['mean']:.0f} qps={r['qps']:.1f}"
                      + (f" first_error={r['first_error']}" if r["first_error"] else ""), flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
