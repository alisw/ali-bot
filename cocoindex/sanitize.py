"""What a published ci-failures.db may not contain: secrets and machine names.

s3snap.publish() runs sanitize_db() on the VACUUM INTO copy of ci-failures.db it
is about to upload, never on the flow's own database: the indexer keeps the
real host names (and its extraction cache keeps the scrubbed log text), and
only what leaves the job is reduced.

In the copy:
* Every text value is scrubbed of secrets again, with logscan.scrub_report().
  The flow already scrubs what it extracts, but its extraction cache can hold
  text scrubbed by older patterns; any hit here is logged, as it means a
  pattern was missing when the text was extracted.
* The host columns name the Nomad allocation instead of the machine:
  alloc-<first 8 hex digits>, or NULL when the log did not record one. An
  operator finds the machine from the allocation in Nomad; a reader of the
  index cannot, and "the same error on one allocation" keeps its meaning.
* In a build's texts, that build's own machine (by full and short name) is
  replaced by its alloc-<id> as well. Any other machine the copy knows of, any
  other *.cern.ch name (but the public services in KEEP_HOSTS), and any
  non-loopback IPv4 address becomes <host> or <ip>.
* Deny by default: only the tables and columns in PUBLISHED leave the job.
  Anything else -- a column a later flow change adds, a table nobody listed --
  is dropped, and the copy is then checked to hold exactly PUBLISHED; if not,
  sanitize_db raises and s3snap does not publish. What is listed is public
  material (repos, PRs, shas, GitHub check names and states), CI metadata
  (times, counts, packages, alloc-<id>), errors by hash (sig_hash,
  class_hash) with a readable `fingerprint` only where its text is public
  (logscan.public_fingerprint: crash fingerprints, compiler diagnostic
  templates), file paths that pass logscan.public_path, and pointers into the
  logs (log_url, line: the logs themselves need CERN SSO). Never log text, no
  signature text (sig_exact, sig_class), and never the full allocation id.
* PRAGMA secure_delete and a final VACUUM, so the replaced and dropped text is
  not left in free pages of the uploaded file.
"""
from __future__ import annotations

import collections
import pathlib
import re
import sqlite3
import sys

_HERE = pathlib.Path(__file__).resolve().parent
for _p in (_HERE, _HERE.parent / "ci-mcp"):
    if (_p / "logscan.py").exists():
        sys.path.insert(0, str(_p))
        break
import logscan  # noqa: E402

HOST = "<host>"
IP = "<ip>"
# Public CERN services, named in errors that say which service failed, and the
# Nomad UI, whose allocation links are what replaces the machine names.
KEEP_HOSTS = {
    "alice-ccdb.cern.ch", "alimonitor.cern.ch", "alien.cern.ch", "s3.cern.ch",
    "gitlab.cern.ch", "auth.cern.ch", "ali-ci.cern.ch", "alinomad.cern.ch",
}
_CERN_RE = re.compile(r"\b(?:[A-Za-z0-9-]+\.)+cern\.ch\b")
# CERN's IPv4 ranges and the private ones; not any dotted quad, as four-part
# version numbers (ROOT 6.36.10.4) look the same.
_IP_RE = re.compile(r"(?<![\w.])(?:128\.14[12]|137\.138|188\.18[45]|10\.\d{1,3}|192\.168"
                    r"|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\w.])")
MIN_SHORT = 6   # bare machine names shorter than this are left alone: too likely a word

# Everything a published ci-failures.db may hold. Adding a column here is a
# decision that its content is public (or a fingerprint, or a pointer).
PUBLISHED = {
    "ci_builds": {
        "build_id", "repo", "pr", "branch", "sha", "check_name", "check_dir", "host", "alidist", "started_at",
        "finished_at", "uploaded_at", "attempt", "current", "gone", "state", "state_source", "gh_state",
        "gh_runs", "gh_failures", "failed_package", "packages", "n_errors", "n_distinct", "infra_kinds",
        "errors_with_file", "errors_in_pr_files", "no_output", "extracted", "log_bytes", "log_key", "log_url"},
    "ci_errors": {
        "build_id", "sig_hash", "rank", "repo", "pr", "sha", "check_name", "host", "uploaded_at", "state",
        "kind", "infra", "fatal", "class_hash", "fingerprint", "file", "package", "section",
        "in_pr_files", "count", "line"},
    # The embedding stays: the MCP server searches with it and never returns it.
    "ci_signatures": {"sig_hash", "class_hash", "kind", "infra", "fingerprint", "file", "embedding"},
    "ci_repos": {"repo", "logs_in_bucket", "builds", "extracted", "pending", "oldest", "newest"},
}


class NotPublishable(RuntimeError):
    pass


def _columns(c: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in c.execute(f'PRAGMA table_info("{table}")')]


def restrict(c: sqlite3.Connection, stats) -> None:
    """Drop every table, view, trigger and index, and every column, not in
    PUBLISHED; then check that what is left is exactly PUBLISHED."""
    for name, kind in c.execute("SELECT name, type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                                "AND type IN ('view', 'trigger', 'index')").fetchall():
        c.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
        stats[f"dropped {kind}"] += 1
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                          "AND name NOT LIKE 'sqlite_%'").fetchall():
        if t not in PUBLISHED:
            c.execute(f'DROP TABLE "{t}"')
            stats["dropped table " + t] += 1
            continue
        for col in _columns(c, t):
            if col not in PUBLISHED[t]:
                c.execute(f'ALTER TABLE "{t}" DROP COLUMN "{col}"')
                stats[f"dropped column {t}.{col}"] += 1
    left = {t: set(_columns(c, t)) for (t,) in c.execute(
        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    extra = {t: sorted(cols - PUBLISHED.get(t, set())) for t, cols in left.items()
             if t not in PUBLISHED or cols - PUBLISHED[t]}
    if extra:
        raise NotPublishable(f"not on the publish list: {extra}")


def _names(host: str) -> list[str]:
    """A machine's full name, and its short one if it is distinctive enough."""
    short = host.split(".", 1)[0]
    return [host] + ([short] if short != host and len(short) >= MIN_SHORT else [])


def _alternation(names) -> re.Pattern | None:
    names = sorted({n for n in names if n}, key=len, reverse=True)
    if not names:
        return None
    return re.compile(r"(?<![A-Za-z0-9.-])(?:" + "|".join(map(re.escape, names)) + r")(?![A-Za-z0-9-]|\.[A-Za-z0-9])")


def alloc_name(alloc: str | None) -> str | None:
    return f"alloc-{alloc[:8]}" if alloc else None


class Sanitizer:
    def __init__(self, hosts):
        self.hosts = {h for h in hosts if h}
        self.any_host = _alternation(n for h in self.hosts for n in _names(h))

    def text(self, value: str, own_host: str | None, own_alloc: str | None, stats) -> str:
        value, kinds = logscan.scrub_report(value)
        for k in kinds:
            stats["secret:" + k] += 1
        if own_host and own_alloc:
            rx = _alternation(_names(own_host))
            value, n = rx.subn(own_alloc, value)
            stats["own host -> alloc"] += n
        if self.any_host:
            value, n = self.any_host.subn(HOST, value)
            stats["other builder -> <host>"] += n
        def cern(m):
            if m.group(0).lower() in KEEP_HOSTS:
                return m.group(0)
            stats["cern.ch name -> <host>"] += 1
            return HOST
        value = _CERN_RE.sub(cern, value)
        value, n = _IP_RE.subn(IP, value)
        stats["ip -> <ip>"] += n
        return value


def _text_columns(c: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in c.execute(f'PRAGMA table_info("{table}")')
            if r[2].upper() in ("TEXT", "") and r[1] != "host"]


def fingerprints(c: sqlite3.Connection, stats) -> None:
    """Add `fingerprint` (the public part of each signature, or NULL) and
    withhold file paths that are not public, before restrict() drops the
    signature text. A published copy (no sig_exact any more) is left alone."""
    for t in ("ci_errors", "ci_signatures"):
        cols = _columns(c, t)
        if "sig_exact" not in cols:
            continue
        if "fingerprint" not in cols:
            c.execute(f'ALTER TABLE "{t}" ADD COLUMN fingerprint TEXT')
        rows = c.execute(f'SELECT rowid, kind, sig_exact, sig_class, file FROM "{t}"').fetchall()
        for rowid, kind, exact, cls, file in rows:
            fp = logscan.public_fingerprint(kind, exact or "", cls or "", file)
            pub = logscan.public_path(file)
            c.execute(f'UPDATE "{t}" SET fingerprint = ?, file = ? WHERE rowid = ?', (fp, pub, rowid))
            stats[f"{t}: readable fingerprint" if fp else f"{t}: hash only"] += 1
            if file and not pub:
                stats[f"{t}: file withheld"] += 1


def sanitize_db(path) -> dict:
    """Sanitize the ci-failures.db at path in place. Returns counts by kind."""
    c = sqlite3.connect(path)
    c.execute("PRAGMA secure_delete = ON")
    stats: collections.Counter = collections.Counter()
    # A DB restored from a snapshot already has alloc-<id> there: not a machine.
    hosts = {h for (h,) in c.execute("SELECT host FROM ci_builds UNION SELECT host FROM ci_errors")
             if h and not h.startswith("alloc-")}
    s = Sanitizer(hosts)
    # A published copy has no alloc column (restrict() dropped it): its hosts are
    # already alloc-<id>.
    has_alloc = "alloc" in _columns(c, "ci_builds")
    build = {b: (h if h in hosts else None, alloc_name(a)) for b, h, a in c.execute(
        f"SELECT build_id, host, {'alloc' if has_alloc else 'NULL'} FROM ci_builds")}

    tables = [t for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                      "AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        cols = _text_columns(c, t)
        has_build = "build_id" in [r[1] for r in c.execute(f'PRAGMA table_info("{t}")')]
        if not cols:
            continue
        sel = ", ".join(f'"{x}"' for x in cols)
        updates = []
        for row in c.execute(f'SELECT rowid, {"build_id, " if has_build else ""}{sel} FROM "{t}"'):
            rowid, rest = row[0], row[1:]
            own_host, own_alloc = build.get(rest[0], (None, None)) if has_build else (None, None)
            vals = rest[1:] if has_build else rest
            new = [s.text(v, own_host, own_alloc, stats) if isinstance(v, str) else v for v in vals]
            if new != list(vals):
                updates.append((rowid, new))
        for rowid, new in updates:
            c.execute(f'UPDATE "{t}" SET ' + ", ".join(f'"{x}" = ?' for x in cols) + " WHERE rowid = ?",
                      (*new, rowid))
        stats[f"rows changed in {t}"] += len(updates)

    # The host columns: the allocation, never the machine.
    if has_alloc:
        c.execute("UPDATE ci_builds SET host = CASE WHEN alloc IS NOT NULL AND alloc != '' "
                  "THEN 'alloc-' || substr(alloc, 1, 8) END")
        c.execute("UPDATE ci_errors SET host = (SELECT b.host FROM ci_builds b "
                  "WHERE b.build_id = ci_errors.build_id)")
    fingerprints(c, stats)
    restrict(c, stats)
    c.commit()
    c.execute("VACUUM")
    c.close()
    return {k: v for k, v in stats.items() if v}
