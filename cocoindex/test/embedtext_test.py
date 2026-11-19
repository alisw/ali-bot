"""embedtext.embed_doc: a text the model reports as too long is retried shorter;
the adaptive limit on texts in flight (AdaptiveLimit) caps, cuts and grows.

    python test/embedtext_test.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import embedtext  # noqa: E402

failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


class ContextWindowExceededError(Exception):
    """Named as litellm's is; embed_doc goes by the name, not the import."""


class Fake:
    def __init__(self, max_chars, error=ContextWindowExceededError,
                 message="This model's maximum context length is 2048 tokens"):
        self.max_chars, self.error, self.message, self.sent = max_chars, error, message, []

    async def embed(self, text):
        self.sent.append(text)
        if len(text) > self.max_chars:
            raise self.error(self.message)
        return len(text)


async def main():
    p = len(embedtext.DOC_PREFIX)
    f = Fake(10_000)
    check(await embedtext.embed_doc(f, "x" * 4000) == p + 4000 and len(f.sent) == 1,
          "a text that fits is sent once, whole, with the document prefix")
    check(f.sent[0].startswith(embedtext.DOC_PREFIX), "the prefix is there")

    f = Fake(2600)
    check(await embedtext.embed_doc(f, "y" * 5000) == p + 2500 and [len(t) - p for t in f.sent] == [5000, 2500],
          "too long: retried at 2500 characters")

    f = Fake(1100)
    check(await embedtext.embed_doc(f, "z" * 5000) == p + 1000 and [len(t) - p for t in f.sent] == [5000, 2500, 1000],
          "still too long: retried at 1000")

    f = Fake(1100)
    check(await embedtext.embed_doc(f, "w" * 2000) == p + 1000 and [len(t) - p for t in f.sent] == [2000, 1000],
          "a limit the text is already under is skipped")

    f = Fake(10)
    try:
        await embedtext.embed_doc(f, "v" * 5000)
        check(False, "too long even at 1000: the error is raised")
    except ContextWindowExceededError:
        check(len(f.sent) == 3, "too long even at 1000: the error is raised")

    class BadRequestError(Exception):
        """litellm's, as llama.cpp's refusal arrives (ALICE's server)."""

    f = Fake(2600, error=BadRequestError, message="litellm.BadRequestError: OpenAIException - Error code: 400 - "
             "input (2130 tokens) is larger than the max context size (2048 tokens). skipping")
    check(await embedtext.embed_doc(f, "t" * 5000) == p + 2500 and [len(t) - p for t in f.sent] == [5000, 2500],
          "llama.cpp's wording (ALICE's server) is recognised too: retried at 2500")

    f = Fake(10, error=RuntimeError, message="Error code: 500 - upstream unavailable")
    try:
        await embedtext.embed_doc(f, "u" * 5000)
        check(False, "another error is raised at once, not retried")
    except RuntimeError:
        check(len(f.sent) == 1, "another error is raised at once, not retried")



class DeadlineExceededError(Exception):
    """Named as CocoIndex's is; the limit goes by the name."""


class Slow:
    """Counts the texts in flight; times out the texts in `fail` (a set of
    texts, each once), else answers after `delay` seconds."""

    def __init__(self, delay=0.01, fail=()):
        self.delay, self.fail, self.inflight, self.peak, self.sent = delay, set(fail), 0, 0, []

    async def embed(self, text):
        self.sent.append(text)
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(self.delay)
            if text in self.fail:
                self.fail.discard(text)
                raise DeadlineExceededError("CocoIndex timeout deadline exceeded")
            return len(text)
        finally:
            self.inflight -= 1


async def limits():
    p = len(embedtext.DOC_PREFIX)
    logs = []
    lim = embedtext.AdaptiveLimit(start=8, lo=2, hi=32, step=2, log=logs.append)
    embedtext._limits[asyncio.get_running_loop()] = lim

    lim.step = 0  # no growth while checking the cap
    s = Slow()
    out = await asyncio.gather(*(embedtext.embed_doc(s, f"t{i}") for i in range(40)))
    lim.step = 2
    check(s.peak == 8 and out == [p + len(f"t{i}") for i in range(40)],
          f"at most 8 texts in flight with a limit of 8 (peak {s.peak}), all answered")
    for i in range(8):
        await embedtext.embed_doc(s, f"u{i}")
    check(lim.limit > 8, f"successes raise the limit ({lim.limit:.2f})")

    lim.limit = 16
    texts = [f"w{i}" for i in range(16)]
    s = Slow(fail=[embedtext.doc(t) for t in texts])  # a whole wave times out once, then answers
    out = await asyncio.gather(*(embedtext.embed_doc(s, t) for t in texts))
    check(all(o == p + len(t) for o, t in zip(out, texts)), "timed-out texts are retried once and answered")
    check(len(s.sent) == 32, "each timed-out text is sent twice, no more")
    cuts = [m for m in logs if "after a timeout" in m]
    check(cuts == ["texts in flight 16 -> 8 after a timeout"],
          f"a wave of timeouts cuts the limit once, 16 -> 8: {cuts}")

    lim.limit, lim.last_cut = 8, float("-inf")
    class Twice(Slow):
        async def embed(self, text):
            self.sent.append(text)
            raise DeadlineExceededError("CocoIndex timeout deadline exceeded")

    t = Twice()
    try:
        await embedtext.embed_doc(t, "y")
        check(False, "a text that times out twice raises")
    except DeadlineExceededError:
        check(len(t.sent) == 2, "a text that times out twice raises after one retry")
    check(int(lim.limit) == 2, f"two waves cut twice, not below the minimum: {int(lim.limit)}")

    s = Slow()
    for i in range(400):
        await embedtext.embed_doc(s, f"g{i}")
    check(int(lim.limit) == 32, f"steady successes grow the limit back to the maximum ({int(lim.limit)})")

    class Other(Slow):
        async def embed(self, text):
            self.sent.append(text)
            raise RuntimeError("Error code: 500")

    o = Other()
    before = lim.limit
    try:
        await embedtext.embed_doc(o, "z")
    except RuntimeError:
        check(len(o.sent) == 1 and lim.limit == before, "another error: no retry, the limit unchanged")

    embedtext.ADAPTIVE = False
    s = Slow()
    await asyncio.gather(*(embedtext.embed_doc(s, f"n{i}") for i in range(50)))
    check(s.peak == 50, "EMBED_ADAPTIVE=0: no limit")
    embedtext.ADAPTIVE = True


asyncio.run(main())
asyncio.run(limits())
print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
