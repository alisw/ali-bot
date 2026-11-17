"""Pure extraction over ALICE CI build logs: what an error looks like, and what it means.

Shared by two readers of the same logs:

  * ci-mcp (ci_mcp_server.py): ci_build_errors / ci_build_log summarise ONE log
    for a human or an agent;
  * cocoindex's ci-failures flow (../cocoindex/ci_failures_flow.py): indexes
    EVERY log in s3://alice-build-logs, groups errors by signature across
    builds, and asks whether a failure is the PR's fault.

It lives here, next to ci-mcp, because ci-mcp is where these rules were tuned
against real logs (and ci/ is the tree deployed onto the builders, which never
run this). Both callers import it by path; the cocoindex jobspecs ship it with
file("../ci-mcp/logscan.py"). It needs nothing outside the standard library and
never touches the network.

Contents:
  patterns()               report-pr-errors' error regexes, lifted by AST (never copied)
  NOISE_RE, FATAL_RE       lines that match a pattern but are not errors / errors that stop a build
  clip, with_body, strip_alibuild_prefix
  scan_lines()             the per-line classification loop both callers run
  INFRA_RULES              evidence-based "the builder broke, not the PR" kinds
  parse_header(), sections(), is_no_output()
  signature()              sig_exact / sig_class of one error, plus the file it names
  scrub()                  secrets out of log text before anything is stored
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import sys
from collections import Counter

MAX_LINE_CHARS = 300

# ---------------------------------------------------------------------------
# Error patterns: imported, never copied.
#
# report-pr-errors has a well-tuned set of regexes -- compiler errors with and
# without ANSI colour, ninja and make failures, CMake, killed compilers, failed
# unit tests, full-system-test timeouts. Reproducing them here would mean two
# definitions of "what an error looks like" drifting apart, and the copy that
# loses is always the one nobody is reading when a new failure mode appears.
# The file has no .py extension, hence the loader dance.
# ---------------------------------------------------------------------------

#: Which of report-pr-errors' regexes to lift, and what to call each kind here.
#:
#: ORDER MATTERS: the first pattern to match wins, and ERRORS_RE is deliberately
#: broad -- it matches ": fatal error:", which is also how a compiler killed by
#: the OOM reaper announces itself ("c++: fatal error: Killed signal terminated
#: program cc1plus"). With the generic pattern first, KILLED_RE never fired and
#: an out-of-memory build was reported as an ordinary compile error, which is a
#: completely different diagnosis. So the specific kinds come first and the
#: catch-all comes last.
WANTED_PATTERNS = {
    "KILLED_RE": "killed",
    "CMAKE_ERROR_RE": "cmake",
    "FAILED_UNIT_TEST_RE": "test",
    "FST_TASK_TIMEOUT_RE": "fst-timeout",
    "FST_LOGFILE_RE": "fst-logfile",
    "FST_FAILED_CMD_RE": "fst-command",
    "ERRORS_RE": "error",
}

FALLBACK_PATTERNS = {"error": re.compile(r": (fatal )?error:|^ERROR: |ninja: build stopped")}


def find_report_pr_errors() -> str | None:
    """report-pr-errors: $LOGSCAN_REPORT_PR_ERRORS, else next to this file (as
    the cocoindex jobspec ships it), else the ali-bot checkout this file is in."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.environ.get("LOGSCAN_REPORT_PR_ERRORS"),
                 os.path.join(here, "report-pr-errors"),
                 os.path.join(os.path.dirname(here), "report-pr-errors")):
        if path and os.path.isfile(path):
            return path
    return None


def load_error_patterns(path: str | None = None) -> dict[str, re.Pattern]:
    """Lift the `NAME_RE = re.compile("...")` assignments out of report-pr-errors.

    Read rather than imported. Importing report-pr-errors means executing it,
    which pulls in alibot_helpers and its whole dependency tree -- so a reader
    would need ali-bot installed in its venv just to learn what an error looks
    like, and would fall back to a worse pattern set the day that broke.

    Only string literals passed to re.compile are evaluated, and only for the
    names listed above, so this reads the file without running any of it.
    """
    path = path or find_report_pr_errors()
    if not path:
        raise RuntimeError("report-pr-errors not found (set LOGSCAN_REPORT_PR_ERRORS)")
    with open(path) as source:
        tree = ast.parse(source.read(), filename=path)
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in WANTED_PATTERNS:
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and call.args):
            continue
        try:
            found[WANTED_PATTERNS[target.id]] = re.compile(ast.literal_eval(call.args[0]))
        except (ValueError, re.error):
            continue          # a pattern we cannot read is one we skip, not a crash
    if "error" not in found:
        raise RuntimeError("ERRORS_RE not found in %s" % path)
    # Re-key in WANTED_PATTERNS order. `found` was filled in the order the
    # assignments appear in report-pr-errors, which puts the broad ERRORS_RE
    # first -- so without this the specific kinds are unreachable no matter how
    # this table is written.
    return {kind: found[kind] for kind in WANTED_PATTERNS.values() if kind in found}


_PATTERNS: dict[str, re.Pattern] | None = None


def patterns() -> dict[str, re.Pattern]:
    """The patterns, loaded once. Lazy, so a reader that only wants signature()
    or scrub() (the MCP server) does not need report-pr-errors at all."""
    global _PATTERNS
    if _PATTERNS is None:
        try:
            _PATTERNS = load_error_patterns()
        except Exception as err:  # noqa: BLE001
            print("could not import report-pr-errors patterns: %s" % err, file=sys.stderr)
            _PATTERNS = dict(FALLBACK_PATTERNS)
    return _PATTERNS


#: Lines that match an error pattern but are not errors.
#:
#: Overwhelmingly the top of any ranking before this existed. aliBuild's
#: prefer_system_check compiles a probe for every optional dependency and prints
#: the compiler's complaint when it is absent -- "WARNING: GSL: <stdin>:1:10:
#: fatal error: gsl/gsl_version.h: No such file or directory". That failure is
#: the mechanism working: it is how aliBuild decides to build its own copy. It
#: says "fatal error" because the compiler said so, and it is prefixed WARNING
#: because aliBuild knows it is not one.
#:
#: The rule is deliberately about the WARNING prefix rather than about probes:
#: an error pattern matching inside a line its own author labelled a warning is
#: a false positive by construction, whatever produced it.
NOISE_RE = re.compile(
    r"^\s*WARNING:"                      # aliBuild's own downgrade, incl. probes
    r"|^\s*report-analytics:"            # telemetry helpers failing to report;
    r"|^\s*report-metric-monalisa:"      # every round, unrelated to the build
    #: recc logs an [ERROR] whenever it declines to remote-execute a command and
    #: falls back to compiling locally -- notably for every link. The build is
    #: fine; on a macOS O2Physics log this alone was 36 of the 36 "errors",
    #: pushing the real failure out of the report.
    r"|\[parsedcommandfactory\.cpp:\d+\] \[ERROR\] Failed to read command-line "
    r"options from file"
)

#: Errors that stop a build, as opposed to errors it survives. Used to rank, not
#: to filter -- a build that dies of one fatal error usually emits dozens of
#: incidental ones first, and frequency alone puts the noisy ones on top.
FATAL_RE = re.compile(
    r"ninja: build stopped:|make.*: \*\*\*|^\s*ERROR:|\[FATAL\]"
    r"|internal compiler error|fatal error: Killed signal")

#: Kinds that are always serious (ranked first).
SERIOUS_KINDS = ("killed", "cmake", "test", "fst-timeout", "fst-logfile", "fst-command")


def clip(line):
    line = line.rstrip("\n")
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " ...[clipped]"


#: How many following lines carry the message, per kind. A compiler puts its
#: complaint on the line that matched, so pulling context there is pure noise.
#: CMake does the opposite: "CMake Error at FetchContent.cmake:2106 (message):"
#: says only WHERE, and every word of WHY is on the lines beneath it -- reported
#: alone it is unactionable, which is exactly how it first came back here.
BODY_LINES = {"cmake": 6, "test": 1, "fst-command": 2, "missing-dependency": 6}


def with_body(lines, position, kind):
    """The matched line plus the continuation lines that carry its meaning."""
    body = [strip_alibuild_prefix(lines[position])]
    for offset in range(1, BODY_LINES.get(kind, 0) + 1):
        if position + offset >= len(lines):
            break
        nxt = strip_alibuild_prefix(lines[position + offset]).strip()
        # CMake indents the body and separates paragraphs with blank lines; stop
        # at "Call Stack", which is location again rather than reason.
        if not nxt or nxt.startswith("Call Stack"):
            break
        body.append("      " + nxt)
    return "\n".join(body)


_ALIBUILD_PREFIX_RE = re.compile(r"^\d{4}-\d\d-\d\d@[\d:]+:[A-Z]+:[^:]*:[^:]*:[^:]*:\s*")


def strip_alibuild_prefix(line):
    """Drop aliBuild's "2026-08-19@21:17:45:DEBUG:O2:GEANT4:slc10_x86-64-...:".

    Two identical compiler errors from two packages are the same error; leaving
    the timestamp on makes every line unique and defeats deduplication.
    """
    return _ALIBUILD_PREFIX_RE.sub("", line)


def scan_lines(lines, kinds=None, extra_noise=None):
    """Classify every line: the loop ci_build_errors and ci_build_log both run.

    Returns (hits, kinds_count, skipped). hits maps a dedup key (kind, line with
    digits collapsed) to [count, sample-with-body, kind, last position]; the
    first pattern of `kinds` (default patterns()) that matches a line wins.
    skipped counts the noise lines (NOISE_RE, plus extra_noise if given).

    ~8-10 MB/s of log per core. (A whole-text multi-line prefilter was tried:
    no faster, as Python's re is slow on these alternations either way. The
    indexer runs this in a process pool instead.)"""
    kinds = patterns() if kinds is None else kinds
    hits, counts, skipped = {}, Counter(), 0
    for position, raw in enumerate(lines):
        line = strip_alibuild_prefix(raw)
        if NOISE_RE.search(line) or (extra_noise is not None and extra_noise.search(line)):
            skipped += 1
            continue
        for kind, pattern in kinds.items():
            if pattern.search(line):
                # Numbers vary between otherwise identical errors (line
                # numbers, template depths, addresses); collapse them so the
                # same failure in twenty files groups into one entry.
                key = (kind, re.sub(r"\d+", "#", line)[:MAX_LINE_CHARS])
                if key in hits:
                    hits[key][0] += 1
                    hits[key][3] = position
                else:
                    hits[key] = [1, with_body(lines, position, kind), kind, position]
                counts[kind] += 1
                break
    return hits, counts, skipped


def severity(entry):
    """Rank fatal-and-late above frequent.

    Sorting by count put ten copies of a harmless message above the two copies
    of the one that stopped the build -- and the decisive error is usually the
    LAST one, since everything after it is teardown. So: the kinds that are
    always serious first, then anything that looks build-stopping, then
    latest-first within each tier. Count is reported but never ranks.
    """
    count, sample, kind, position = entry
    if kind in SERIOUS_KINDS or kind in INFRA_KINDS:
        tier = 0
    elif FATAL_RE.search(sample):
        tier = 1
    else:
        tier = 2
    return (tier, -position)


# ---------------------------------------------------------------------------
# "The builder broke, not the PR": evidence-based kinds.
#
# Each rule was kept only if it showed up in real logs. Counts are from the
# tails of all 1,595 non-empty failed builds in s3://alice-build-logs on
# 2026-11-16 (8 weeks of CI), plus 301 "no output" logs:
#   no-output            301  < 2000 bytes and "No logs found": the build never ran
#   gpu-unavailable       42  the GPU builder could not load its GPU backend
#   disk-full             33  ld/as/clang "No space left on device" (18 on one Mac check)
#   remote-cache          16  recc: "Failed to download output blob ... Blob absent"
#   missing-system-dep   ~75  CMake: a system package the builder image lacks
#                             (GLUT 31, GIF 23, LibLZMA 19, ...)
#   alidist-sync          29  "FindO2GPU.cmake differs in O2 compared to alidist"
# Not seen in 8 weeks, but kept because report-pr-errors/ci-mcp encode them:
# killed (OOM: "fatal error: Killed signal terminated program") and the FST
# timeouts. Network/DNS/git-fetch failures did not appear in fullLog.txt at
# all: report-pr-errors puts the fetch log into pretty.html, not fullLog.txt.
# alidist-sync is listed but is not infra in the strict sense: it says O2 and
# alidist disagree, which a PR touching FindO2GPU.cmake can cause itself.
# ---------------------------------------------------------------------------
INFRA_RULES = {
    "disk-full": re.compile(r"No space left on device|errno=28\b"),
    "gpu-unavailable": re.compile(r"Could not load GPUReconstruction for specified device"
                                  r"|GPU detection failed"),
    "remote-cache": re.compile(r"Failed to download output blob .*Blob absent"),
    "missing-system-dep": re.compile(r"The required package \S+ was not found\.\s+Please install it"
                                     r"|^\s*Could NOT find \S+ \(missing:"
                                     r"|REQUIRED package\(s\) are missing"),
    "alidist-sync": re.compile(r"differs in \S+ compared to alidist, please sync"),
}
INFRA_KINDS = frozenset(INFRA_RULES) | {"no-output", "killed"}


def classify_kinds() -> dict[str, re.Pattern]:
    """Infra rules first, then report-pr-errors' kinds (specific before generic)."""
    return {**INFRA_RULES, **patterns()}


#: A green ctest summary matches FAILED_UNIT_TEST_RE ("% tests passed"); it is
#: not an error. Used by the indexer, not by ci-mcp (which shows it on purpose).
GREEN_SUMMARY_RE = re.compile(r"\b100% tests passed, 0 tests failed")

#: What the indexer skips on top of NOISE_RE: the green ctest summary, and bash
#: xtrace lines ("+ echo 'FindO2GPU.cmake differs ...'"), which are commands
#: being run, not their output -- the output line follows and is what counts.
INDEX_NOISE_RE = re.compile(GREEN_SUMMARY_RE.pattern + r"|^\++ \S")


# ---------------------------------------------------------------------------
# The log's own structure (written by report-pr-errors' cat())
# ---------------------------------------------------------------------------
_HOST_RE = re.compile(r"^Finished building on (\S+) at (\d{4}-\d\d-\d\d-\d\d:\d\d:\d\d)", re.M)
_BUILT_RE = re.compile(r"^Built commit (\S+) from https://github\.com/([^/\s]+/[^/\s]+)/pull/(\S+)", re.M)
_ALLOC_RE = re.compile(r"/ui/allocations/([0-9a-f]{8}-[0-9a-f-]{27})")
_ALIDIST_RE = re.compile(r"^Built with alidist ([0-9a-f]{40})$", re.M)  # since 2026-11; absent before
_SECTION_RE = re.compile(r"^## Begin (?:\S*/)?BUILD/(\S+?)-latest[^/\s]*/log\b")


def is_no_output(text: str) -> bool:
    """A few hundred bytes ending in "No logs found" is not a failed PR, it is a
    broken machine: the build produced nothing at all."""
    return len(text) < 2000 and "No logs found" in text


def parse_header(text: str) -> dict:
    """host, finished_at (builder-local 'YYYY-MM-DD HH:MM:SS', no zone), sha,
    repo, ref (PR number or branch), alloc, alidist (the alidist commit used;
    only in logs written since report-pr-errors records it). Missing fields
    are None."""
    head = text[:4000]
    out = {"host": None, "finished_at": None, "sha": None, "repo": None, "ref": None, "alloc": None,
           "alidist": None}
    m = _HOST_RE.search(head)
    if m:
        d = m.group(2)
        out["host"], out["finished_at"] = m.group(1), d[:10] + " " + d[11:]
    m = _BUILT_RE.search(head)
    if m:
        out["sha"], out["repo"], out["ref"] = m.group(1), m.group(2), m.group(3)
    m = _ALLOC_RE.search(head)
    if m:
        out["alloc"] = m.group(1)
    m = _ALIDIST_RE.search(head)
    if m:
        out["alidist"] = m.group(1)
    return out


def sections(lines) -> list[tuple[str, int]]:
    """[(package, first line index)] of each "## Begin sw/BUILD/<pkg>-latest/log"."""
    out = []
    for i, line in enumerate(lines):
        if line.startswith("## Begin "):
            m = _SECTION_RE.match(line)
            out.append((m.group(1) if m else line.split()[2], i))
    return out


def package_at(secs, position) -> str | None:
    pkg = None
    for name, start in secs:
        if start > position:
            break
        pkg = name
    return pkg


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------
_ESC_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\[[0-9;]{1,12}m")
# Where sources and installs live on the builders, longest first. The capture is
# the aliBuild package; what follows is the path inside its source tree.
_SRC_PREFIX_RE = re.compile(
    r"(?:/[^\s:'\"()]*?)?/sw/SOURCES/([A-Za-z0-9_.+-]+)/[^/\s]+/[^/\s]+/")
_INSTALL_PREFIX_RE = re.compile(
    r"(?:/[^\s:'\"()]*?)?/sw/(?:[a-z]+\d*_[a-z0-9_-]+)/([A-Za-z0-9_.+-]+)/[^/\s]+/")
_BUILD_DIR_RE = re.compile(r"(?:/[^\s:'\"()]*?)?/sw/BUILD/[0-9a-f]{40}/([A-Za-z0-9_.+-]+)/")
_TMP_RE = re.compile(r"(/(?:private/)?(?:var/folders|tmp)/)[^\s:'\"()]*")
_TS_RE = re.compile(r"\b\d{4}-\d\d-\d\d[T@ ]\d\d:\d\d(:\d\d)?(\.\d+)?([+-]\d{2}:?\d{2}|Z)?"
                    r"|\[\d\d:\d\d:\d\d\]|\b\d{4}-\d\d-\d\d\b")
_LINECOL_RE = re.compile(r"(\.[A-Za-z0-9_+]+|\b[A-Za-z0-9_]+|\))(?::\d+)(?::\d+)?(?=[:,)\s]|$)")
_PAREN_LINE_RE = re.compile(r"(\.[A-Za-z0-9_+]+)\(\d+\)(?=:)")
_HEX_RE = re.compile(r"\b(?:0x)?[0-9a-f]{7,64}\b")
_PR_RE = re.compile(r"(pull/|#)\d{2,6}\b|/SOURCES/[^/\s]+/\d+/")
_PID_RES = [(re.compile(r"\b(pid|PID|port)\s*[=:]?\s*\d+"), r"\1 N"),
            (re.compile(r"\[\d+:(?:0x[0-9a-f]+|\d+)\]"), "[N:N]"),        # recc/buildbox [pid:thread]
            (re.compile(r"\[\d+:([\w.-]+)\]"), r"[\1]"),
            (re.compile(r"\b(127\.0\.0\.1|localhost):\d+"), r"\1:N")]
_NINJA_RE = re.compile(r"\[\d+/\d+\]")
_HOST_NAME_RE = re.compile(r"\b[A-Za-z0-9-]+\.cern\.ch\b")
_TESTNUM_RE = re.compile(r"\b\d+/\d+ Test +#\d+:|Test +#\d+:|\b\d+\.\d+ sec\b|\.{3,}")
_TESTSUM_RE = re.compile(r"\d+% tests passed, \d+ tests failed out of \d+")
_GREP_PREFIX_RE = re.compile(r"^\d+[-:](?=[^\d\s])")   # "6366-[ERROR] ...", not "2026-10-20T..."
_WAE_RE = re.compile(r",-warnings-as-errors(?=\])|\[-Werror,(-W[\w-]+)\]")
_PATH = (r"(?:[A-Za-z0-9_.+-]+:)?[A-Za-z0-9_.+-]*/[A-Za-z0-9_./+-]+\.[A-Za-z0-9_+]+"
         r"|\b[A-Za-z0-9_+-]+\.(?:cxx|cpp|cc|c|h|hpp|hxx|cu|C|cmake|py|sh|txt|inc)")
_FILE_RE = re.compile(r"(" + _PATH + r")(?=[:(])")
# The file an error is ABOUT: compilers and clang-tidy start the line with it
# ("path:line:col: error:", nvcc/boost "path(line): error"), CMake says "at path:line".
_FILE_AT_RE = re.compile(r" at (" + _PATH + r")(?=:\d)")
_FILE_AT_START_RE = re.compile(r"^(?:CMake (?:Error|Warning)(?: \(dev\))? at |In file included from )?"
                               r"(" + _PATH + r")(?=[:(])")
_QUOTED_RE = re.compile(r"(?<![A-Za-z0-9])'[^'\n]*'|\"[^\"\n]*\"|‘[^’\n]*’")
_DIGITS_RE = re.compile(r"\b\d+\b")
_WS_RE = re.compile(r"[ \t]+")


def _relocate(line: str) -> tuple[str, str | None]:
    """Strip builder-specific prefixes: '/sw/SOURCES/O2Physics/<arch>/0/PWGLF/x.cxx'
    -> 'PWGLF/x.cxx' (package O2Physics); an installed '/sw/<arch>/Eigen3/<ver>/include/..'
    -> 'Eigen3:include/..'; a build dir '/sw/BUILD/<hash>/O2/..' -> 'O2:..'.
    Returns (line, package of the first source path, or None)."""
    pkg = None
    m = _SRC_PREFIX_RE.search(line)
    if m:
        pkg = m.group(1)
    line = _SRC_PREFIX_RE.sub(lambda mm: "", line)
    line = _BUILD_DIR_RE.sub(lambda mm: mm.group(1) + ":", line)
    line = _INSTALL_PREFIX_RE.sub(lambda mm: mm.group(1) + ":", line)
    return line, pkg


#: Kinds whose continuation lines are part of the message (CMake says only
#: WHERE on the matched line). Elsewhere the body is context (a test's next
#: log line, with hosts and pids in it) and stays out of the signature.
SIG_BODY_KINDS = {"cmake", "missing-system-dep"}


def normalize(text: str, kind: str | None = None) -> tuple[str, str | None, str | None]:
    """(sig_exact text, file, package) of one error (first line + body, for the
    kinds whose body carries the message)."""
    out, file, pkg = [], None, None
    lines = text.splitlines()
    if kind is not None and kind not in SIG_BODY_KINDS:
        lines = lines[:1]
    for i, line in enumerate(lines):
        line = _ESC_RE.sub("", strip_alibuild_prefix(line)).strip()
        line = _GREP_PREFIX_RE.sub("", line)
        line, p = _relocate(line)
        if i == 0:
            pkg = p
            m = _FILE_AT_START_RE.match(line) or _FILE_AT_RE.search(line)
            if m:
                file = m.group(1)
        line = _TMP_RE.sub(r"\1<tmp>", line)
        line = _TS_RE.sub("<time>", line)
        line = _PAREN_LINE_RE.sub(r"\1", line)
        line = _LINECOL_RE.sub(r"\1", line)
        line = _TESTNUM_RE.sub(lambda m: "Test:" if "Test" in m.group(0) else " ", line)
        line = _TESTSUM_RE.sub("N% tests passed, N tests failed out of N", line)
        line = _PR_RE.sub(lambda m: (m.group(1) or "/SOURCES/x/") + ("N" if m.group(1) else ""), line)
        for rx, repl in _PID_RES:
            line = rx.sub(repl, line)
        line = _NINJA_RE.sub("[N/N]", line)
        line = _HOST_NAME_RE.sub("<host>", line)
        line = _HEX_RE.sub("<hex>", line)
        line = _WAE_RE.sub(lambda m: f"[{m.group(1)}]" if m.group(1) else "", line)
        line = _WS_RE.sub(" ", line).strip()
        if line:
            out.append(line)
    if file:
        file = _LINECOL_RE.sub(r"\1", file)
    return "\n".join(out)[:2000], file, pkg


def _class_of(exact: str, file: str | None) -> str:
    """The message template: the file path, quoted names and numbers removed."""
    first, _, rest = exact.partition("\n")
    if file and first.startswith(file):
        first = first[len(file):].lstrip(":( ),")
    elif file:
        first = first.replace(file, "<file>", 1)
    first = _FILE_RE.sub("<file>", first)
    s = first + ("\n" + rest if rest else "")
    s = _QUOTED_RE.sub("'_'", s)
    s = _DIGITS_RE.sub("N", s)
    return _WS_RE.sub(" ", s).strip()


def sig_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def signature(text: str, kind: str | None = None) -> dict:
    """The two-level signature of one error (as returned by with_body):

    sig_exact: the error with build-path prefixes, line:col, hex hashes, tmp
      paths, PR numbers, pids/ports and timestamps stripped, the file path kept
      relative to its package's source tree;
    sig_class: the same with the file path, quoted names and numbers removed --
      the message template ("C-style casts are discouraged; use static_cast
      [google-readability-casting]").
    """
    exact, file, pkg = normalize(text, kind)
    cls = _class_of(exact, file)
    return {"sig_exact": exact, "sig_hash": sig_hash(exact), "sig_class": cls,
            "class_hash": sig_hash(cls), "file": file, "package": pkg}


# ---------------------------------------------------------------------------
# Secrets. Log text is produced by PR code and builder scripts; anything that
# looks like a credential is replaced before it is stored or embedded.
# ---------------------------------------------------------------------------
REDACTED = "<redacted>"
# (kind, pattern, replacement). The kind is only for reporting (scrub_report).
_SECRET_RES = [
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
     "-----BEGIN PRIVATE KEY-----" + REDACTED + "-----END PRIVATE KEY-----"),
    ("pgp-private-key", re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----.*?(-----END PGP PRIVATE KEY BLOCK-----|\Z)", re.S),
     "-----BEGIN PGP PRIVATE KEY BLOCK-----" + REDACTED + "-----END PGP PRIVATE KEY BLOCK-----"),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), REDACTED),
    ("github-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}"), REDACTED),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), REDACTED),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), REDACTED),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), REDACTED),
    # OpenAI-style keys, which LiteLLM gateways such as aigw also hand out.
    ("sk-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), REDACTED),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), REDACTED),
    ("auth-header", re.compile(r"(?i)\b(authorization|proxy-authorization|x-vault-token|x-api-key|private-token)"
                               r"(\s*[:=]\s*)[^\r\n]*"), r"\1\2" + REDACTED),
    ("key-value", re.compile(r"(?i)\b((?:[a-z0-9]+[_-])*(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key)"
                             r"(?:[_-][a-z0-9]+)*)(\s*[=:]\s*)(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&'\"]+)"),
     r"\1\2" + REDACTED),
    ("url-credentials", re.compile(r"\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + REDACTED + "@"),
    ("url-credentials", re.compile(r"\b([a-z][a-z0-9+.-]*://)[^/\s:@]{16,}@"), r"\1" + REDACTED + "@"),
]


def scrub_report(text: str) -> tuple[str, list[str]]:
    """scrub(), and the kind of each pattern that changed the text."""
    kinds = []
    for kind, rx, repl in _SECRET_RES:
        new = rx.sub(repl, text)
        if new != text:
            kinds.append(kind)
            text = new
    return text, kinds


def scrub(text: str) -> str:
    return scrub_report(text)[0]


# ---------------------------------------------------------------------------
# One log, as the indexer keeps it (top-level, so a process pool can run it)
# ---------------------------------------------------------------------------
def extract_log(raw: bytes, max_errors: int = 40, text_chars: int = 1500) -> dict:
    """What the ci-failures indexer keeps of one fullLog.txt: its header, the
    aliBuild sections, and up to max_errors distinct errors, ranked as ci-mcp
    ranks them (severity), each signed and with its text clipped. Everything
    kept -- each error's text with its body lines, and the header -- is scrubbed
    of secrets before it is signed or stored. (Scrubbing the whole log first
    did the same and cost a third of the extraction time on 10 MB logs.)"""
    text = raw.decode("utf-8", errors="replace")
    hdr = {k: (scrub(v) if isinstance(v, str) else v) for k, v in parse_header(text).items()}
    if is_no_output(text):
        sig = signature("No logs found: the build produced no output", "no-output")
        return {"header": hdr, "no_output": 1, "packages": [], "failed_package": None, "n_errors": 1,
                "n_distinct": 1, "errors": [dict(sig, kind="no-output", infra=1, fatal=1, count=1, rank=1,
                                                 text="No logs found. Please check the aurora log.")]}
    lines = text.splitlines()
    secs = sections(lines)
    hits, counts, _ = scan_lines(lines, classify_kinds(), INDEX_NOISE_RE)
    ranked = sorted(hits.values(), key=severity)
    errors: dict[str, dict] = {}
    for count, sample, kind, pos in ranked:
        sample = scrub(sample)
        sig = signature(sample, kind)
        e = errors.get(sig["sig_hash"])
        if e:  # two raw lines that only differed in what the signature strips
            e["count"] += count
            continue
        if len(errors) >= max_errors:
            continue
        fatal = int(kind in SERIOUS_KINDS or kind in INFRA_KINDS
                    or bool(FATAL_RE.search(sample)))
        errors[sig["sig_hash"]] = dict(
            sig, kind=kind, infra=int(kind in INFRA_KINDS), fatal=fatal, count=count,
            rank=len(errors) + 1, section=package_at(secs, pos),
            text="\n".join(clip(l) for l in sample.splitlines())[:text_chars])
    first_fatal = next((e for e in errors.values() if e["fatal"]), None)
    return {"header": hdr, "no_output": 0, "packages": [s[0] for s in secs],
            "failed_package": (first_fatal or {}).get("section") or (secs[-1][0] if secs else None),
            "n_errors": sum(counts.values()), "n_distinct": len(hits), "errors": list(errors.values())}
