"""Gauges for the cocoindex indexer and its MCP server, pushed to MONIT (Grafana).

The same OTLP/HTTP gauges as ci/otlp-push.py (queue-metrics, bb-cache-metrics):
one metric per field, named <name>_<field>, with the tags as attributes; MONIT
stores them as Prometheus series. The endpoint is the security-proxy's
monit-otlp route: OTLP_METRICS_URL and OTLP_WRITE_TOKEN (a gate token, swapped
for the MONIT credential upstream), or a function returning both.

Nothing here may slow or break what it measures: with no endpoint nothing is
sent, a failed push is logged (once per kind of failure) and dropped, and the
pushes run in a daemon thread. No user names, queries or log text are ever an
attribute: tools, flows and routes only; callers are counted, never named.

    metrics.py push NAME TAG=V ... -- FIELD=V ...   one point, as otlp-push.py
    metrics.py probe ROUTE...                      embed one short text per route
                                                   (PROBE_<ROUTE>_URL/_TOKEN), push
                                                   cocoindex_embed_probe
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("cocoindex-metrics")
SERVICE = os.environ.get("METRICS_SERVICE", "cocoindex")
INTERVAL_S = float(os.environ.get("METRICS_INTERVAL_S", "60"))


# ---------------------------------------------------------------- sending
def payload(points: list[tuple[str, dict, float]], now_ns: int | None = None) -> dict:
    """points: (metric name, attributes, value) -> an OTLP/JSON request of gauges."""
    now = str(now_ns or time.time_ns())
    return {"resourceMetrics": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": SERVICE}}]},
        "scopeMetrics": [{"scope": {"name": "ali-bot/cocoindex"}, "metrics": [
            {"name": n, "gauge": {"dataPoints": [{
                "timeUnixNano": now, "asDouble": float(v),
                "attributes": [{"key": k, "value": {"stringValue": str(a)}} for k, a in sorted(attrs.items())]}]}}
            for n, attrs, v in points]}]}]}


def env_endpoint() -> tuple[str, str] | None:
    url, token = os.environ.get("OTLP_METRICS_URL"), os.environ.get("OTLP_WRITE_TOKEN")
    return (url, token or "") if url else None


_warned: set[str] = set()


def send(points, endpoint=None) -> bool:
    """POST the points; False (logged once per kind of failure) if they did not land."""
    if not points:
        return True
    try:
        ep = endpoint() if callable(endpoint) else (endpoint or env_endpoint())
    except Exception as e:      # the proxy route is missing or not ready
        ep, why = None, f"no endpoint: {type(e).__name__}"
        if why not in _warned:
            _warned.add(why)
            log.warning("metrics: %s; not sending", why)
    if not ep:
        return False
    url, token = ep
    req = urllib.request.Request(url, data=json.dumps(payload(points)).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(2048).decode("utf-8", "replace").strip()
        if body and body not in ("{}", '{"partialSuccess":{}}'):
            raise OSError(f"endpoint reported: {body[:200]}")
        return True
    except (OSError, ValueError) as e:
        why = getattr(e, "code", None) or type(e).__name__
        if why not in _warned:
            _warned.add(why)
            log.warning("metrics: push failed (%s: %s); dropping, reported once", why, str(e)[:200])
        return False


# ---------------------------------------------------------------- aggregation
def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


class Window:
    """Counts and observations since the last flush, by (metric, attributes).

    flush() turns them into gauges of that window: <m>_count, <m>_per_s and,
    for observations, <m>_p50/_p95/_max. A window with nothing in it pushes
    zeros for the counters it has seen before, so a quiet minute reads 0, not
    a gap."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: dict[tuple, float] = {}
        self._obs: dict[tuple, list[float]] = {}
        self._seen: set[tuple] = set()
        self._gauges: dict[tuple, float] = {}
        self._since = time.monotonic()

    @staticmethod
    def _key(metric, attrs):
        return (metric, tuple(sorted(attrs.items())))

    def count(self, metric: str, n: float = 1, **attrs) -> None:
        k = self._key(metric, attrs)
        with self._lock:
            self._counts[k] = self._counts.get(k, 0) + n

    def observe(self, metric: str, value: float, **attrs) -> None:
        k = self._key(metric, attrs)
        with self._lock:
            self._obs.setdefault(k, []).append(value)

    def gauge(self, metric: str, value: float, **attrs) -> None:
        """A level, pushed as it is at every flush until set again."""
        with self._lock:
            self._gauges[self._key(metric, attrs)] = value

    def flush(self) -> list[tuple[str, dict, float]]:
        with self._lock:
            now = time.monotonic()
            span = max(now - self._since, 1e-3)
            counts, obs, self._counts, self._obs, self._since = self._counts, self._obs, {}, {}, now
            for k in self._seen:
                counts.setdefault(k, 0)
            self._seen |= set(counts)
            gauges = dict(self._gauges)
        out = []
        for (m, a), n in counts.items():
            out += [(m + "_count", dict(a), n), (m + "_per_s", dict(a), n / span)]
        for (m, a), xs in obs.items():
            out += [(m + "_p50", dict(a), _pct(xs, .5)), (m + "_p95", dict(a), _pct(xs, .95)),
                    (m + "_max", dict(a), max(xs))]
        out += [(m, dict(a), v) for (m, a), v in gauges.items()]
        return out


class Pusher:
    """Flushes a Window every INTERVAL_S from a daemon thread, and once at exit."""

    def __init__(self, window: Window, endpoint=None, interval: float = INTERVAL_S, before=None):
        self.window, self.endpoint, self.interval, self.before = window, endpoint, interval, before
        self._stop = threading.Event()
        threading.Thread(target=self._loop, name="metrics", daemon=True).start()
        atexit.register(self.flush)

    def flush(self) -> None:
        try:
            if self.before:
                self.before(self.window)
            send(self.window.flush(), self.endpoint)
        except Exception as e:      # never let a metric take the process down
            log.warning("metrics: flush failed: %s: %s", type(e).__name__, e)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.flush()


# ---------------------------------------------------------------- the flows' embedder
def install_litellm(flow: str, route: str | None = None) -> Window | None:
    """Count every embedding request litellm makes in this process (cocoindex's
    LiteLLMEmbedder goes through litellm.aembedding): requests started,
    succeeded and failed, texts embedded, and request latency. Started minus
    finished is what cocoindex abandoned at its deadline (EMBED_TIMEOUT_S)."""
    if not env_endpoint():
        return None
    try:
        import litellm
        from litellm.integrations.custom_logger import CustomLogger
    except ImportError:
        return None
    route = route or os.environ.get("EMBED_ROUTE", "aigw")
    w = Window()
    tags = dict(flow=flow, route=route)

    def texts(kwargs) -> int:
        x = kwargs.get("input")
        return len(x) if isinstance(x, list) else 1

    class Counter(CustomLogger):
        def log_pre_api_call(self, model, messages, kwargs):
            # litellm calls only this one (not the async variant) for aembedding
            w.count("cocoindex_embed_requests_started", **tags)

        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
            w.count("cocoindex_embed_requests_ok", **tags)
            w.count("cocoindex_embed_texts", texts(kwargs), **tags)
            w.observe("cocoindex_embed_request_seconds", (end_time - start_time).total_seconds(), **tags)

        async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
            w.count("cocoindex_embed_requests_failed", **tags)

    litellm.callbacks = [*getattr(litellm, "callbacks", []), Counter()]
    Pusher(w)
    return w


# ---------------------------------------------------------------- CLI
def _pairs(args):
    sep = args.index("--")
    kv = lambda items: dict(i.split("=", 1) for i in items)
    return args[0], kv(args[1:sep]), kv(args[sep + 1:])


def probe(routes: list[str]) -> list[tuple[str, dict, float]]:
    """One short text per route, timed: a continuous view of each embedder,
    apart from the indexer's own load on it."""
    import random
    model = os.environ.get("EMBED_MODEL", "openai/embeddinggemma-300m").split("/", 1)[-1]
    out = []
    for r in routes:
        key = r.upper().replace("-", "_")
        url, token = os.environ.get(f"PROBE_{key}_URL"), os.environ.get(f"PROBE_{key}_TOKEN", "")
        if not url:
            continue
        text = f"probe {random.random():.6f}: TPC calibration failed for an empty time frame"   # not cached upstream
        req = urllib.request.Request(url.rstrip("/") + "/embeddings", method="POST",
                                     data=json.dumps({"model": model, "input": [text]}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        t0, ok = time.monotonic(), 0
        try:
            with urllib.request.urlopen(req, timeout=float(os.environ.get("PROBE_TIMEOUT_S", "60"))) as resp:
                ok = int(bool(json.load(resp)["data"][0]["embedding"]))
        except Exception as e:
            log.warning("probe %s: %s: %s", r, type(e).__name__, str(e)[:200])
        out.append(("cocoindex_embed_probe_seconds", {"route": r}, time.monotonic() - t0))
        out.append(("cocoindex_embed_probe_ok", {"route": r}, ok))
    return out


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="metrics: %(message)s")
    if argv[:1] == ["push"] and "--" in argv:
        name, tags, fields = _pairs(argv[1:])
        return 0 if send([(f"{name}_{f}", tags, float(v)) for f, v in fields.items()]) else 1
    if argv[:1] == ["probe"]:
        pts = probe(argv[1:])
        for n, a, v in pts:
            print(f"{n}{a} {v:.3f}")
        return 0 if send(pts) else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
