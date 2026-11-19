"""The task prefixes embeddinggemma was trained with, in one place.

Every text the flows embed is a document, and every search text is a query:

    document:  "title: none | text: <text>"
    query:     "task: search result | query: <text>"

Both sides must agree with what is stored, so the indexer (commits_flow.py,
prs_flow.py, ci_failures_flow.py) and the MCP server (reader.py) import this
module rather than spelling the prefixes out. Changing either string changes
every vector: the index must then be rebuilt from scratch.

Measured on 400 O2 commits (2026-11-17; query = subject line, document = the
rest of the message, embeddinggemma-300m on aigw): MRR 0.791 bare, 0.810 with
both prefixes, 0.802 with only the document one.

Every embed_doc call also passes through an adaptive limit on the texts a flow
has in flight (AdaptiveLimit below), so that a slow embedding server gets less
load instead of a wall of timeouts.
"""

import asyncio
import math
import os
import sys
import time

DOC_PREFIX = "title: none | text: "
QUERY_PREFIX = "task: search result | query: "


def doc(text: str) -> str:
    """A text to store: a commit message, a diff chunk, a PR text, a CI error."""
    return DOC_PREFIX + text


def query(text: str) -> str:
    """A search text, compared against stored documents."""
    return QUERY_PREFIX + text


# Texts are cut to a character budget before they are embedded, but the model's
# limit is 2048 TOKENS, and aigw answers 400 (litellm's ContextWindowExceededError)
# rather than truncating. Ordinary text and code run 3-4 characters per token; a
# squashed-commit log of hashes and dates, or hundreds of "* 1" WIP bullets, runs
# nearer 2, and 5000 characters of it does not fit. Such a text is retried shorter.
# ALICE's server (llama.cpp) says it differently, as a plain BadRequestError:
# "input (2130 tokens) is larger than the max context size (2048 tokens)". Until
# 2026-11-18 that was not recognised, and the whole PR failed (11 in one pass).
RETRY_CHARS = (2500, 1000)


def _too_long(e: BaseException) -> bool:
    m = str(e)
    return ("ContextWindowExceeded" in type(e).__name__ or "maximum context length" in m
            or "larger than the max context size" in m)


# Adaptive limit on the texts in flight (2026-11-19). The flows hand CocoIndex
# every text of 16 components at once, ~600 texts, and CocoIndex sends them in
# requests of 64, at most EMBED_MAX_INFLIGHT_REQUESTS at a time; each request's
# EMBED_TIMEOUT_S deadline also runs while it waits for a slot. When the server
# slows down (shared GPUs: 15 s for one text on 2026-11-19), nearly every request
# then times out, and each timeout fails a whole commit: ~600 lost commits in a
# 50-minute run that morning, on either server. So texts wait here instead, for a
# limit that behaves like TCP's congestion window: halved by a timeout, raised by
# EMBED_LIMIT_STEP for every window of successes. A timed-out text is retried once
# under the lower limit. Timeouts of one wave (a request of 64 texts fails as one)
# cut the limit once: only a request started after the last cut can cut it again.
# EMBED_ADAPTIVE=0 turns it off. The limit is per process, i.e. per flow.
ADAPTIVE = os.environ.get("EMBED_ADAPTIVE", "1") != "0"
LIMIT_START = float(os.environ.get("EMBED_LIMIT_START", "64"))
LIMIT_MIN = float(os.environ.get("EMBED_LIMIT_MIN", "4"))
LIMIT_MAX = float(os.environ.get("EMBED_LIMIT_MAX",
                                 str(64 * int(os.environ.get("EMBED_MAX_INFLIGHT_REQUESTS", "4")))))
LIMIT_STEP = float(os.environ.get("EMBED_LIMIT_STEP", "2"))


def _log(msg: str) -> None:
    print(f"embedtext: {msg}", file=sys.stderr, flush=True)


def _timed_out(e: BaseException) -> bool:
    n = type(e).__name__
    return isinstance(e, TimeoutError) or "DeadlineExceeded" in n or "Timeout" in n


class AdaptiveLimit:
    """At most int(limit) texts in flight; additive increase, multiplicative decrease."""

    def __init__(self, start=LIMIT_START, lo=LIMIT_MIN, hi=LIMIT_MAX, step=LIMIT_STEP, log=_log):
        self.lo, self.hi, self.step, self.log = lo, hi, step, log
        self.limit = min(hi, max(lo, start))
        self.inflight = 0
        self.last_cut = -math.inf
        self.cond = asyncio.Condition()

    async def acquire(self) -> float:
        async with self.cond:
            await self.cond.wait_for(lambda: self.inflight < int(self.limit))
            self.inflight += 1
        return time.monotonic()

    async def release(self) -> None:
        async with self.cond:
            self.inflight -= 1
            self.cond.notify_all()

    def success(self) -> None:
        old = int(self.limit)
        self.limit = min(self.hi, self.limit + self.step / self.limit)
        if int(self.limit) // 16 > old // 16:
            self.log(f"texts in flight up to {int(self.limit)}")

    def timeout(self, started: float) -> bool:
        """A request started at `started` timed out; True if that cut the limit."""
        if started < self.last_cut:
            return False
        old = int(self.limit)
        self.limit = max(self.lo, self.limit / 2)
        self.last_cut = time.monotonic()
        self.log(f"texts in flight {old} -> {int(self.limit)} after a timeout")
        return True


_limits: dict = {}


def _limit():
    """This event loop's AdaptiveLimit (one per flow process), or None if off."""
    if not ADAPTIVE:
        return None
    loop = asyncio.get_running_loop()
    if loop not in _limits:
        _limits[loop] = AdaptiveLimit()
    return _limits[loop]


async def _embed(embedder, text: str):
    lim = _limit()
    if lim is None:
        return await embedder.embed(text)
    for attempt in (1, 2):
        started = await lim.acquire()
        try:
            v = await embedder.embed(text)
        except Exception as e:
            if _timed_out(e):
                lim.timeout(started)
                if attempt == 1:
                    continue
            raise
        finally:
            await lim.release()
        lim.success()
        return v
    raise AssertionError("unreachable")


async def embed_doc(embedder, text: str):
    """embedder.embed(doc(text)), retried on RETRY_CHARS-long prefixes of text if
    the model reports it as too long, and once more after a timeout, under the
    adaptive limit. Any other error is raised as it is."""
    for limit in (None, *RETRY_CHARS):
        if limit is not None and len(text) <= limit:
            continue
        try:
            return await _embed(embedder, doc(text if limit is None else text[:limit]))
        except Exception as e:
            if not _too_long(e) or limit == RETRY_CHARS[-1]:
                raise
    raise AssertionError("unreachable")
