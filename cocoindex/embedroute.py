"""Which embedding route to use: the first that answers and gives the index's vectors.

Every stored vector comes from EMBED_MODEL as aigw serves it (the canonical
route). Another server may only be used if it gives the same vectors: a
different conversion of the same model can give good embeddings in another
space (2026-11-18: an embeddinggemma GGUF without its dense layers had a
median cosine of 0.002 to aigw), which would silently corrupt the index. So a
candidate route embeds a fixed reference text, and is used only if its vector
is within MIN_COSINE of the canonical route's vector for that text. The
canonical vector is kept in REF_FILE, so a candidate can still be checked
while aigw itself is down; it is refreshed whenever aigw answers.

    embedroute.py pick ROUTE...      prints the route to use (the canonical
                                     one if no other qualifies)

Per route R (upper case, '-' as '_'): ROUTE_<R>_URL (an OpenAI-compatible base,
.../v1) and ROUTE_<R>_TOKEN. EMBED_MODEL as the flows. EMBED_CANONICAL_ROUTE
(default aigw), EMBED_PICK_TIMEOUT_S (default 20), EMBED_REF_FILE.
"""
from __future__ import annotations

import json
import os
import sys
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


def embed(route: str, text: str, timeout: float, lookup=None) -> list[float]:
    """route's normalised embedding of text; lookup(route) -> (base, token) or
    None, by default from ROUTE_<R>_URL/_TOKEN (the MCP server asks its proxy)."""
    ep = (lookup or endpoint)(route)
    if not ep:
        raise RuntimeError(f"no endpoint for route {route}")
    url, token = ep
    req = urllib.request.Request(url.rstrip("/") + "/embeddings", method="POST",
                                 data=json.dumps({"model": model(), "input": [text]}).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        v = json.load(r)["data"][0]["embedding"]
    n = sum(x * x for x in v) ** 0.5
    if not v or not n:
        raise RuntimeError(f"{route}: empty embedding")
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) if len(a) == len(b) else -1.0


def ref_file() -> Path:
    return Path(os.environ.get("EMBED_REF_FILE") or Path(os.environ.get("COCO_ROOT", ".")) / "embed-reference.json")


def load_ref() -> list[float] | None:
    try:
        d = json.loads(ref_file().read_text())
        return d["vector"] if d.get("model") == model() and d.get("text") == REF_TEXT else None
    except (OSError, ValueError, KeyError):
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

    def pick(self, routes: list[str], timeout: float) -> str:
        for r in routes:
            why = self.check(r, timeout)
            if why == "ok":
                return r
            log(f"{r}: {why}; trying the next route")
        log(f"no route qualified; using {CANONICAL}")
        return CANONICAL


def main(argv: list[str]) -> int:
    if argv[:1] != ["pick"] or len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    timeout = float(os.environ.get("EMBED_PICK_TIMEOUT_S", "20"))
    print(Picker().pick(argv[1:], timeout))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
