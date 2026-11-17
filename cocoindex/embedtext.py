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
"""

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
RETRY_CHARS = (2500, 1000)


def _too_long(e: BaseException) -> bool:
    return "ContextWindowExceeded" in type(e).__name__ or "maximum context length" in str(e)


async def embed_doc(embedder, text: str):
    """embedder.embed(doc(text)), retried on RETRY_CHARS-long prefixes of text if
    the model reports it as too long. Any other error is raised as it is."""
    for limit in (None, *RETRY_CHARS):
        if limit is not None and len(text) <= limit:
            continue
        try:
            return await embedder.embed(doc(text if limit is None else text[:limit]))
        except Exception as e:
            if not _too_long(e) or limit == RETRY_CHARS[-1]:
                raise
    raise AssertionError("unreachable")
