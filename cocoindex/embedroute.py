"""Which embedding route to use: the fastest that answers and gives the index's vectors.

Every stored vector comes from EMBED_MODEL as aigw serves it (the canonical
route). Another server may only be used if it gives the same vectors: a
different conversion of the same model can give good embeddings in another
space (2026-11-18: an embeddinggemma GGUF without its dense layers had a
median cosine of 0.002 to aigw), which would silently corrupt the index. So a
candidate route embeds a fixed reference text, and is used only if its vector
is within MIN_COSINE of the canonical route's vector for that text. The
canonical vector is kept in REF_FILE, so a candidate can still be checked
while aigw itself is down; it is refreshed whenever aigw answers.

A running flow re-times the routes every EMBED_REPICK_S and moves to a clearly
faster one (start_repicker, repick, use_route).

    embedroute.py pick ROUTE...      prints the route to use: all are asked for
                                     the reference text at once, and the first
                                     answer that qualifies wins -- without
                                     waiting for the others (a hanging aigw
                                     costs nothing once another route has
                                     answered); the canonical one if none does

Per route R (upper case, '-' as '_'): ROUTE_<R>_URL (an OpenAI-compatible base,
.../v1) and ROUTE_<R>_TOKEN. EMBED_MODEL as the flows. EMBED_CANONICAL_ROUTE
(default aigw), EMBED_PICK_TIMEOUT_S (default 20), EMBED_REF_FILE.
"""
from __future__ import annotations

import json
import os
import queue
import random
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import embedtext  # noqa: E402

CANONICAL = os.environ.get("EMBED_CANONICAL_ROUTE", "aigw")
MIN_COSINE = 0.999
REF_TEXT = embedtext.doc("AliceO2Group/AliceO2: Fix TPC cluster merging across sector boundaries")


def log(*a):
    print("embedroute:", *a, file=sys.stderr, flush=True)


def endpoint(route: str) -> tuple[str, str] | None:
    k = route.upper().replace("-", "_")
    url = os.environ.get(f"ROUTE_{k}_URL")
    return (url, os.environ.get(f"ROUTE_{k}_TOKEN", "")) if url else None


def model() -> str:
    return os.environ.get("EMBED_MODEL", "openai/embeddinggemma-300m").split("/", 1)[-1]


def embed(route: str, text: str, timeout: float, lookup=None, fresh: bool = False) -> list[float]:
    """route's normalised embedding of text; lookup(route) -> (base, token) or
    None, by default from ROUTE_<R>_URL/_TOKEN (the MCP server asks its proxy).
    fresh: send a random sentence along in the same request, so that a server
    caching answers (aigw is a LiteLLM gateway) cannot answer from its cache:
    on 2026-11-18 aigw answered the fixed reference text in 0.4 s while it
    took 42 s on average, or never answered, for any new text."""
    ep = (lookup or endpoint)(route)
    if not ep:
        raise RuntimeError(f"no endpoint for route {route}")
    url, token = ep
    # The fresh sentence carries the query prefix, as every text sent does.
    inputs = [text] + ([embedtext.query(f"route check {random.random():.12f}: the time a new text takes")]
                       if fresh else [])
    req = urllib.request.Request(url.rstrip("/") + "/embeddings", method="POST",
                                 data=json.dumps({"model": model(), "input": inputs}).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        v = min(json.load(r)["data"], key=lambda d: d.get("index", 0))["embedding"]
    n = sum(x * x for x in v) ** 0.5
    if not v or not n:
        raise RuntimeError(f"{route}: empty embedding")
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) if len(a) == len(b) else -1.0


def ref_file() -> Path:
    return Path(os.environ.get("EMBED_REF_FILE") or Path(os.environ.get("COCO_ROOT", ".")) / "embed-reference.json")


#: aigw's vector for REF_TEXT, recorded once and shipped with the code: a fresh
#: process (the MCP server after a restart) can then check a candidate route
#: even if aigw has not answered since it started -- exactly when the candidate
#: is needed (2026-11-18: aigw hung, and the MCP server, never having seen an
#: aigw answer, kept the ALICE server blocked).
SHIPPED_REF = Path(os.environ.get("EMBED_SHIPPED_REF_FILE") or Path(__file__).resolve().parent / "embed-reference.json")


def load_ref() -> list[float] | None:
    """The kept reference (REF_FILE, refreshed whenever aigw answers), else
    the shipped one; neither, or one for another model or text: None."""
    for p in (ref_file(), SHIPPED_REF):
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if d.get("model") == model() and d.get("text") == REF_TEXT and d.get("vector"):
            return d["vector"]
    return None


def save_ref(v: list[float]) -> None:
    p = ref_file()
    try:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"model": model(), "text": REF_TEXT, "vector": v, "saved": time.time()}))
        tmp.replace(p)
    except OSError as e:
        log(f"could not keep the reference vector: {e}")


class Picker:
    """pick(routes): the first route that answers and whose vectors are the
    canonical route's. Keeps the canonical vector (in memory, and in REF_FILE
    when persist) so a candidate is checked even while the canonical route is
    down. The MCP server keeps one Picker; the runner runs `pick` per flow."""

    def __init__(self, persist: bool = True, lookup=None):
        self.persist, self.lookup = persist, lookup
        self.ref = load_ref() if persist else None

    def reference(self, timeout: float) -> list[float] | None:
        try:
            v = embed(CANONICAL, REF_TEXT, timeout, self.lookup)
        except Exception as e:
            log(f"{CANONICAL}: no answer for the reference text ({type(e).__name__}: {str(e)[:120]})")
            return self.ref
        self.ref = v
        if self.persist:
            save_ref(v)
        return v

    def check(self, route: str, timeout: float) -> str:
        """'ok', or why the route is not to be used."""
        try:
            v = embed(route, REF_TEXT, timeout, self.lookup)
        except Exception as e:
            return f"no answer ({type(e).__name__}: {str(e)[:120]})"
        if route == CANONICAL:
            self.ref = v
            if self.persist:
                save_ref(v)
            return "ok"
        ref = self.ref if self.ref is not None else self.reference(timeout)
        if ref is None:
            return f"cannot be checked: no reference vector from {CANONICAL} yet"
        c = cosine(v, ref)
        return "ok" if c >= MIN_COSINE else f"vectors differ from {CANONICAL}'s (cosine {c:.4f} < {MIN_COSINE})"

    def _timed(self, route: str, timeout: float):
        t0 = time.monotonic()
        try:
            return embed(route, REF_TEXT, timeout, self.lookup, fresh=True), time.monotonic() - t0, None
        except Exception as e:
            return None, time.monotonic() - t0, f"no answer ({type(e).__name__}: {str(e)[:120]})"

    def pick(self, routes: list[str], timeout: float) -> str:
        """All routes embed the reference text at once; the first answer that
        qualifies wins, without waiting for the others: it is the fastest, and
        a pick is per flow (minutes to an hour), so a route slow now is likely
        slow for the flow too. Daemon threads, so a route still hanging does
        not keep the process alive. A candidate that answers before any
        reference vector exists waits for the canonical answer to be checked
        against, and still wins if it passes: it was faster."""
        answers: queue.Queue = queue.Queue()
        for r in routes:
            threading.Thread(target=lambda r=r: answers.put((r, *self._timed(r, timeout))), daemon=True).start()
        unchecked = []                 # candidates that answered before any reference existed
        deadline = time.monotonic() + timeout + 5
        for _ in routes:
            try:
                r, v, dt, err = answers.get(timeout=max(0.1, deadline - time.monotonic()))
            except queue.Empty:
                break
            if err:
                log(f"{r}: {err}")
                continue
            if r == CANONICAL:
                self.ref = v
                if self.persist:
                    save_ref(v)
                for r2, v2, dt2 in unchecked:
                    if cosine(v2, v) >= MIN_COSINE:
                        log(f"{r2} answered first ({dt2:.1f} s), checked against {r}; using {r2}")
                        return r2
                    log(f"{r2}: vectors differ from {CANONICAL}'s (cosine {cosine(v2, v):.4f} < {MIN_COSINE})")
                log(f"{r} answered first ({dt:.1f} s); using {r}")
                return r
            if self.ref is None:
                unchecked.append((r, v, dt))
                continue
            c = cosine(v, self.ref)
            if c < MIN_COSINE:
                log(f"{r}: vectors differ from {CANONICAL}'s (cosine {c:.4f} < {MIN_COSINE})")
                continue
            log(f"{r} answered first ({dt:.1f} s); using {r}")
            return r
        for r2, _, _ in unchecked:
            log(f"{r2}: cannot be checked: no reference vector from {CANONICAL} yet")
        log(f"no route qualified; using {CANONICAL}")
        return CANONICAL

#: How often a running flow re-times the routes (EMBED_REPICK_S; 0: never).
REPICK_S = float(os.environ.get("EMBED_REPICK_S", "600"))
#: A route replaces the current one only when at least this much faster: the
#: current route carries the flow's own requests, so it always looks slower,
#: and a smaller margin would flip between routes every few minutes.
SWITCH_FACTOR = 2.0
#: ...and by at least this many seconds: 0.3 s against 0.1 s is not worth a move.
SWITCH_MIN_GAIN_S = 1.0


def repick(picker: Picker, routes: list[str], current: str, timeout: float) -> tuple[str | None, dict]:
    """Times every route at once with a fresh text and checks each against the
    reference vector. Returns (the route to switch to, or None to stay; the
    qualifying routes' times). Waits for all: the current route's time is
    the point of the comparison."""
    results: dict = {}
    threads = [threading.Thread(target=lambda r=r: results.__setitem__(r, picker._timed(r, timeout)), daemon=True)
               for r in routes]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 5)
    if (c := results.get(CANONICAL)) and c[0] is not None:
        picker.ref = c[0]
        if picker.persist:
            save_ref(c[0])
    ok = {}
    for r, (v, dt, err) in list(results.items()):
        if v is not None and (r == CANONICAL or (picker.ref is not None and cosine(v, picker.ref) >= MIN_COSINE)):
            ok[r] = dt
    if not ok:
        return None, ok
    best = min(ok, key=ok.get)
    if best == current:
        return None, ok
    if current not in ok or (ok[best] * SWITCH_FACTOR < ok[current]
                             and ok[current] - ok[best] >= SWITCH_MIN_GAIN_S):
        return best, ok
    return None, ok


def use_route(route: str) -> bool:
    """Points this process's embeddings at route: litellm reads OPENAI_API_BASE
    and OPENAI_API_KEY at every request (they are kept out of the embedder's
    memo key for that reason), so nothing cached is invalidated."""
    ep = endpoint(route)
    if not ep:
        return False
    os.environ["OPENAI_API_BASE"], os.environ["OPENAI_API_KEY"] = ep[0], ep[1] or "unused"
    os.environ["EMBED_ROUTE"] = route
    return True


def start_repicker(flow: str) -> threading.Thread | None:
    """In a flow's process: every REPICK_S, re-time the routes in EMBED_ROUTES
    and move the flow's embeddings to a clearly faster one. The runner picks
    once per flow, from one race at its start; both servers change speed by
    the minute, and on 2026-11-18 a pick at a bad moment kept a 50-minute
    commits pass on alice-embed at 0.5 texts/s (64 commits past their deadline)
    while aigw had done 1.1 texts/s the pass before."""
    routes = [r for r in os.environ.get("EMBED_ROUTES", "").split(",") if r]
    if len(routes) < 2 or REPICK_S <= 0 or not os.environ.get("OPENAI_API_BASE"):
        return None
    timeout = float(os.environ.get("EMBED_PICK_TIMEOUT_S", "20"))

    def loop():
        picker = Picker()
        while True:
            time.sleep(REPICK_S)
            current = os.environ.get("EMBED_ROUTE", CANONICAL)
            try:
                new, times = repick(picker, routes, current, timeout)
            except Exception as e:  # noqa: BLE001 -- never take the flow down
                log(f"{flow}: re-pick failed: {e}")
                continue
            shown = ", ".join(f"{r} {t:.1f} s" for r, t in sorted(times.items(), key=lambda x: x[1])) or "none answered"
            if new and use_route(new):
                log(f"{flow}: embeddings move {current} -> {new} ({shown})")
            else:
                log(f"{flow}: embeddings stay on {current} ({shown})")

    t = threading.Thread(target=loop, daemon=True, name=f"repick-{flow}")
    t.start()
    return t


def main(argv: list[str]) -> int:
    if argv[:1] != ["pick"] or len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    timeout = float(os.environ.get("EMBED_PICK_TIMEOUT_S", "20"))
    print(Picker().pick(argv[1:], timeout))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
