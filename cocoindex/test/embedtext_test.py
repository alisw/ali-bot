"""embedtext.embed_doc: a text the model reports as too long is retried shorter.

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


asyncio.run(main())
print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
