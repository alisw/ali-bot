"""metrics.py: the gauges sent to MONIT, and that sending them never gets in the way.

    python test/metrics_test.py
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import fake_otlp  # noqa: E402
import metrics  # noqa: E402

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


# ---- a window: counts, rates, percentiles, levels
w = metrics.Window()
for x in range(1, 101):
    w.observe("t_seconds", x / 100, tool="a")
w.count("t_calls", 3, tool="a")
w.gauge("t_level", 7)
got = {(n, tuple(a.items())): v for n, a, v in w.flush()}
check(got[("t_seconds_p50", (("tool", "a"),))] == 0.51 and got[("t_seconds_p95", (("tool", "a"),))] == 0.96
      and got[("t_seconds_max", (("tool", "a"),))] == 1.0, "observations become p50, p95 and max of the window")
check(got[("t_calls_count", (("tool", "a"),))] == 3 and got[("t_calls_per_s", (("tool", "a"),))] > 0,
      "counts become a count and a rate")
got = {n: v for n, a, v in w.flush()}
check(got.get("t_calls_count") == 0 and "t_seconds_p50" not in got and got.get("t_level") == 7,
      f"a quiet window reads 0 for a counter seen before, no percentile, and the level again ({got})")

# ---- sending
bodies: list = []
port = free_port()
fake_otlp.serve(port, bodies)
url = f"http://127.0.0.1:{port}/otlp/v1/metrics"
check(metrics.send([("x_y", {"flow": "f"}, 1.5)], (url, "gate")) and
      fake_otlp.points(bodies) == [("x_y", {"flow": "f"}, 1.5)], "a point lands as an OTLP gauge with its attributes")
t0 = time.monotonic()
check(metrics.send([("x", {}, 1)], ("http://127.0.0.1:9/none", "")) is False and time.monotonic() - t0 < 5,
      "an unreachable endpoint returns False at once, without raising")
check(metrics.send([("x", {}, 1)], lambda: (_ for _ in ()).throw(RuntimeError("no route"))) is False,
      "an endpoint function that fails (no proxy route) returns False, without raising")
os.environ.pop("OTLP_METRICS_URL", None)
check(metrics.send([("x", {}, 1)]) is False and metrics.install_litellm("commits") is None,
      "without OTLP_METRICS_URL nothing is sent and nothing is installed")

env = dict(os.environ, OTLP_METRICS_URL=url, OTLP_WRITE_TOKEN="gate")
n0 = len(bodies)
r = subprocess.run([sys.executable, str(HERE.parent / "metrics.py"), "push", "cocoindex_flow", "flow=prs", "--",
                    "seconds=12", "timed_out=0"], env=env, capture_output=True, text=True)
check(r.returncode == 0 and sorted(fake_otlp.points(bodies[n0:])) ==
      [("cocoindex_flow_seconds", {"flow": "prs"}, 12.0), ("cocoindex_flow_timed_out", {"flow": "prs"}, 0.0)],
      f"the CLI push, as runner.sh calls it ({r.stderr.strip()[:200]})")

# ---- the embedder probe: one good route, one dead one
eport = free_port()
emb = subprocess.Popen([sys.executable, str(HERE / "fake_embedder.py"), str(eport), os.devnull])
try:
    time.sleep(0.5)
    n0 = len(bodies)
    r = subprocess.run([sys.executable, str(HERE.parent / "metrics.py"), "probe", "aigw", "aligrid"],
                       env=dict(env, PROBE_AIGW_URL=f"http://127.0.0.1:{eport}/v1", PROBE_AIGW_TOKEN="t",
                                PROBE_ALIGRID_URL="http://127.0.0.1:9/v1", PROBE_TIMEOUT_S="2"),
                       capture_output=True, text=True)
    pts = {(n, a["route"]): v for n, a, v in fake_otlp.points(bodies[n0:])}
    check(pts.get(("cocoindex_embed_probe_ok", "aigw")) == 1 and pts.get(("cocoindex_embed_probe_ok", "aligrid")) == 0
          and pts.get(("cocoindex_embed_probe_seconds", "aigw"), 99) < 5,
          f"the probe times one embedding per route, and a dead route reads ok=0 ({pts})")
finally:
    emb.terminate()

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
