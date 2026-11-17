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
