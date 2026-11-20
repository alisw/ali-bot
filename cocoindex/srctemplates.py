"""Public templates for runtime error messages, from the source that prints them.

A runtime message ("[FATAL] Got nullptr from CCDB for path Users/m/meide/...
and timestamp 1500") is hash-only in the published index, because its values
can name people, paths and hosts. Its *format string* is public code:

    reportFatal(std::string("Got nullptr from CCDB for path ") + path +
                std::string(" and timestamp ") + std::to_string(timestamp));

Matching the message to that statement gives a template that is public by
construction -- its literal pieces, every value a hole "_" -- and a permalink
to the line that prints it. The values (the "fillers") are never published:
normalised (numbers, pointers, hashes, dates, pids, tmp paths out) and hashed
with a key, they tell apart two failures of one template ("which CCDB object")
without saying what they are. So an error is grouped three ways: sig_hash
(the exact line), template_id (the message), template_id + filler_hash (the
message with the same arguments).

Templates come from the format strings of LOG*/LOGP/LOGF, throw ...(...),
ROOT's Error/Fatal/Warning(location, fmt, ...) and any call whose name says it
reports (reportFatal, printError, ...), in the working trees runner.sh keeps
under $COCO_ROOT/sources (shallow, sparse: C++ sources and CMake files only).
Boost.Test failures ("error: in "<case>": check <expr> has failed") and ctest
failures ("Test: <name> ***Failed") are public when the test file, case,
expression or test name are found in those trees.

    python srctemplates.py build <sources dir> <owner/repo>=<branch> ...

writes <sources dir>/templates.json, unless every head is unchanged.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time

EXTS = (".cxx", ".cpp", ".cc", ".h", ".hpp", ".hxx", ".C", ".inl", ".icc")
SH_EXTS, PY_EXTS = (".sh",), (".py",)
SKIP_DIRS = {".git", "build", "doc", "docs", "tutorials", "roottest", "test_data", "interpreter"}
INDEX = "templates.json"
INDEX_VERSION = 6   # 2: signposts, ROOT-named calls read both ways; 3: shell, Python, ROOT class prefix;
                    # 4: no comments, no function definitions; 5: one template per line of a
                    # message, Python's %r and <...log...>.write(); 6: the public vocabulary

CALL_RE = re.compile(
    r"\b(?P<log>LOG|LOGP|LOGF|LOG_IF|LOGP_IF|LOGF_IF|ILOG|QcInfoLogger::\w+|throw\s+(?:[\w:]+::)?"
    r"(?:runtime_error|invalid_argument|logic_error|out_of_range|length_error|domain_error|range_error"
    r"|system_error|bad_\w+|runtime_error_f|Exception|\w*Error|\w*Exception))\s*\("
    r"|\b(?P<root>Error|Fatal|Warning|SysError|Break)\s*\("
    # O2's signposts: (log, id, name, format, args...), the format is the last literal.
    r"|\b(?P<signpost>O2_SIGNPOST_\w+)\s*\("
    # Any helper that reports: reportFatal(...), printError(...), throwException(...).
    # Every template is literal text from public source, so a wider net cannot
    # leak anything; at worst it attributes a message to the wrong call.
    r"|\b(?P<gen>\w*(?:[Rr]eport|[Ff]atal|[Ee]rror|[Ww]arn|[Ee]xception|[Mm]essage|[Ff]ail|[Pp]rint)\w*)\s*\(")
STR_RE = re.compile(r'(?:u8|L|u|U)?"((?:[^"\\\n]|\\.)*)"')
FMT_RE = re.compile(r"%\{[^{}]*\}[A-Za-z]|\{[^{}]*\}|%[-+ #0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?(?:hh|h|ll|l|L|q|j|z|t)?[diouxXeEfFgGaAcspnr]")
HOLE = "\x00"
CLS = "\x01"   # ROOT prints Error() locations as "<Class::method>": an optional, unshown class prefix
SPECIAL = (HOLE, CLS)
MIN_ANCHOR = 8          # the longest literal piece must be at least this long
MIN_LITERAL = 12        # and the literal pieces together at least this long


# ---------------------------------------------------------------- extraction
def _unescape(s: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def _statement(src: str, start: int) -> str | None:
    """From a call's start to its ';' at depth 0, skipping string and char literals."""
    depth, i, n = 0, start, min(len(src), start + 3000)
    while i < n:
        ch = src[i]
        if ch == '"':
            m = STR_RE.match(src, i)
            if not m:
                return None
            i = m.end()
            continue
        if ch == "'":
            j = src.find("'", i + 1 + (src[i + 1:i + 2] == "\\"))
            i = j + 1 if j > 0 else i + 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return src[start:i]
        elif ch == ";" and depth == 0:
            return src[start:i]
        i += 1
    return None


# C++ comments, skipped over string, character and raw string literals. Code
# that is commented out prints nothing.
_CXX_TOKEN_RE = re.compile(r'R"([^()\\\s]{0,16})\(.*?\)\1"|"(?:[^"\\\n]|\\.)*"' r"|'(?:[^'\\\n]|\\.)*'"
                           r"|//[^\n]*|/\*.*?(?:\*/|$)", re.S)


def _uncomment(src: str) -> str:
    """src with every comment blanked, newlines kept (so line numbers hold)."""
    return _CXX_TOKEN_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)) if m.group(0)[0] == "/" else m.group(0), src)


def _is_definition(src: str, open_paren: int) -> bool:
    """printError(...) { ... }: a function's definition, not a call. Its body
    would read as one long statement of every literal in it."""
    depth, i, n = 1, open_paren, min(len(src), open_paren + 3000)
    while i < n and depth:
        ch = src[i]
        if ch == '"':
            m = STR_RE.match(src, i)
            i = m.end() if m else i + 1
            continue
        depth += ch == "("
        depth -= ch == ")"
        i += 1
    return bool(_DEF_TAIL_RE.match(src, i))


_DEF_TAIL_RE = re.compile(r"\s*(?:(?:const|override|final|noexcept(?:\([^)]*\))?|volatile|&&?)\s*)*"
                          r"(?:->\s*[^{;=]+?)?\s*\{")


def _pieces(stmt: str, head_end: int, root_call: str | None, last_only: bool = False) -> list[str] | None:
    """The message as literal pieces and HOLEs, or None."""
    lits = list(STR_RE.finditer(stmt, head_end))
    if not lits:
        return None
    if last_only:
        lits = lits[-1:]
        head_end = lits[0].start()      # the arguments before the format are not printed
    out: list[str] = []
    if root_call:
        # ROOT: Error(location, fmt, ...) prints "Error in <location>: fmt".
        first = STR_RE.match(stmt[head_end:].lstrip())
        if first and len(lits) >= 2:
            loc, lits = _unescape(first.group(1)), lits[1:]
        elif first:
            return None     # Error("message", ...): not ROOT's (location, format)
        else:
            loc = HOLE
        if sum(len(_unescape(m.group(1)).strip()) for m in lits) < MIN_LITERAL:
            return None     # "Error in <_>: _": the prefix alone says nothing
        word = {"SysError": "SysError", "Break": "*** Break ***"}.get(root_call, root_call)
        out = [f"{word} in <", CLS, loc, ">: "]
        prev = lits[0].start()
    else:
        prev = head_end
    has_fmt = False
    for k, m in enumerate(lits):
        code = re.sub(r"<<|\+|\s|,|\(|\)|fmt::format|std::string|Form|TString::Format|"
                      r"fmt::runtime|o2::utils::Str::concat_string", "", stmt[prev:m.start()])
        if (k > 0 or not root_call) and code and not (k == 0 and re.fullmatch(r"\w+", code) and not root_call):
            out.append(HOLE)        # an expression between two literals: a value
        parts = FMT_RE.split(_unescape(m.group(1)))
        has_fmt |= len(parts) > 1
        for j, p in enumerate(parts):
            if j:
                out.append(HOLE)
            out.append(p)
        prev = m.end()
    if re.sub(r"\s|\)|;", "", stmt[prev:]) and not has_fmt:
        out.append(HOLE)            # "... " << value;  "... " + std::to_string(t))
    return _merge(out)


def _merge(out: list[str]) -> list[str]:
    """Adjacent literals joined, empty ones dropped, runs of holes collapsed."""
    merged: list[str] = []
    for p in out:
        if p == "":
            continue
        if merged and p not in SPECIAL and merged[-1] not in SPECIAL:
            merged[-1] += p
        elif not (p == HOLE and merged and merged[-1] == HOLE):
            merged.append(p)
    return merged


# Shell: echo/printf (and the usual wrappers) of a double-quoted string; its
# variables and command substitutions are values.
SH_CALL_RE = re.compile(r"\b(?:echo\w*|printf|die|fail\w*|err\w*|log\w*|warn\w*|fatal\w*)\b(?:\s+-[neE]+)*"
                        r'\s+"((?:[^"\\]|\\.)*)"')
SH_VAR_RE = re.compile(r"\$\{[^}]*\}|\$\([^)]*\)|\$\w+|\$[0-9@*#?]")


def _sh_pieces(lit: str, printf: bool) -> list[str]:
    out: list[str] = []
    for j, part in enumerate(SH_VAR_RE.split(_unescape(lit))):
        if j:
            out.append(HOLE)
        for k, q in enumerate(FMT_RE.split(part) if printf else [part]):
            if k:
                out.append(HOLE)
            out.append(q)
    return _merge(out)


# Python: print(...), raise X(...), logging/logger calls and helpers whose name
# says they report; f-string {...} and %-formats are values, as is any code
# between two literals.
PY_CALL_RE = re.compile(r"\b(?:print|raise\s+[\w.]+|(?:logging|logger|log|self\.\w*log\w*)\.\w+|\w*log\w*\.write"
                        r"|\w*(?:[Ee]rror|[Ww]arn|[Ff]atal|[Ff]ail|[Dd]ie)\w*)\s*\(")
PY_STR_RE = re.compile(r"""[rRbBuUfF]{0,2}("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')""")


def _py_args(src: str, start: int) -> str | None:
    """From after a call's "(" to its matching ")", skipping string literals."""
    depth, i, n = 1, start, min(len(src), start + 3000)
    while i < n:
        ch = src[i]
        if ch in "\"'":
            m = PY_STR_RE.match(src, i)
            if not m:
                return None
            i = m.end()
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return src[start:i]
        i += 1
    return None


def _after_first_arg(args: str) -> str | None:
    """A call's arguments without the first one (up to its top-level comma)."""
    depth, i = 0, 0
    while i < len(args):
        ch = args[i]
        if ch in "\"'":
            m = PY_STR_RE.match(args, i)
            if not m:
                return None
            i = m.end()
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            return args[i + 1:]
        i += 1
    return None


def _py_pieces(args: str) -> list[str] | None:
    lits = list(PY_STR_RE.finditer(args))
    if not lits:
        return None
    out: list[str] = []
    prev, has_fmt = 0, False
    for k, m in enumerate(lits):
        code = re.sub(r"[\s+,()%]|\.format|str", "", args[prev:m.start()])
        if code:
            out.append(HOLE)        # an expression before or between literals: a value
        parts = FMT_RE.split(_unescape(m.group(1)[1:-1]))
        has_fmt |= len(parts) > 1
        for j, q in enumerate(parts):
            if j:
                out.append(HOLE)
            out.append(q)
        prev = m.end()
    tail = re.sub(r"[\s,)]", "", args[prev:]).split("=")[0]
    if not has_fmt and tail not in ("", "file", "flush", "end", "sep"):
        out.append(HOLE)
    return _merge(out)


def _norm_literal(s: str) -> str:
    """What logscan.normalize does to the literal text of a message."""
    s = re.sub(r"\b[A-Za-z0-9-]+\.cern\.ch\b", "<host>", s)
    return re.sub(r"[ \t]+", " ", s)


def _walk(root: str):
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for f in files:
            yield d, f


def _keep(pcs: list[str] | None) -> list[str] | None:
    if not pcs:
        return None
    pcs = [x if x in SPECIAL else _norm_literal(x) for x in pcs]
    lit = [x.strip() for x in pcs if x not in SPECIAL]
    if not lit or sum(map(len, lit)) < MIN_LITERAL or max(map(len, lit)) < MIN_ANCHOR:
        return None
    return pcs


def _keep_lines(pcs: list[str] | None) -> list[list[str]]:
    """One template per line of a message: logscan matches a log line by line,
    so a literal holding newlines ("%s command for package %r failed.\\n
    Command: %s ...", aliBuild's workarea.py) would never match as a whole.
    A message with only a trailing newline gives the one template it gave
    before. Lines too short to anchor a match are dropped (_keep)."""
    if not pcs:
        return []
    lines: list[list[str]] = [[]]
    for p in pcs:
        if p in SPECIAL or "\n" not in p:
            lines[-1].append(p)
            continue
        first, *rest = p.split("\n")
        lines[-1].append(first)
        for q in rest:
            lines.append([q])
    return [k for k in (_keep(_merge(l)) for l in lines) if k]


def extract(repo: str, root: str) -> list[dict]:
    out = []
    for d, f in _walk(root):
        if not f.endswith(EXTS + SH_EXTS + PY_EXTS):
            continue
        p = os.path.join(d, f)
        try:
            src = open(p, errors="replace").read()
        except OSError:
            continue
        rel = os.path.relpath(p, root)
        if f.endswith(SH_EXTS):
            for m in SH_CALL_RE.finditer(src):
                for pcs in _keep_lines(_sh_pieces(m.group(1), src[m.start():m.start() + 6] == "printf")):
                    out.append({"repo": repo, "path": rel, "line": src.count("\n", 0, m.start()) + 1, "pieces": pcs})
            continue
        if f.endswith(PY_EXTS):
            for m in PY_CALL_RE.finditer(src):
                args = _py_args(src, m.end())
                if args is not None and m.group(0).startswith("dieOnError"):
                    args = _after_first_arg(args)   # aliBuild's dieOnError(condition, message)
                for pcs in _keep_lines(_py_pieces(args)) if args is not None else ():
                    out.append({"repo": repo, "path": rel, "line": src.count("\n", 0, m.start()) + 1, "pieces": pcs})
            continue
        if not CALL_RE.search(src):
            continue
        src = _uncomment(src)
        for m in CALL_RE.finditer(src):
            if _is_definition(src, m.end()):
                continue
            st = _statement(src, m.start())
            if not st:
                continue
            head = m.end() - m.start()
            if m.group("signpost"):
                variants = [_pieces(st, head, None, last_only=True)]
            elif m.group("root"):
                # ROOT's Error(location, fmt, ...) -- or a local Error(message, ...)
                # of the same name, as rootcling's LinkdefReader has. The plain
                # reading only when the first argument reads as a message.
                variants = [_pieces(st, head, m.group("root"))]
                first = STR_RE.match(st[head:].lstrip())
                if first and " " in first.group(1):
                    variants.append(_pieces(st, head, None))
            else:
                variants = [_pieces(st, head, None)]
            line = src.count("\n", 0, m.start()) + 1
            for pcs in (k for v in variants for k in _keep_lines(v)):
                out.append({"repo": repo, "path": rel, "line": line, "pieces": pcs})
    return out


# ---------------------------------------------------------------- vocabulary
# What a value (a filler) may be shown as: the names of the code of
# NAME_OWNERS' repos -- whole identifiers, comments and string literals out, and
# CMake's unquoted words (the workflows' targets) -- by name_key(), and whole
# string literals of every tree. Whole names, not the words in them: a surname
# is a word of some identifier more often than one is a name. See public_filler().
NAME_OWNERS = ("AliceO2Group/", "FairRootGroup/")
MAX_LITERAL = 200
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PY_STR_RE = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'')
_HASH_COMMENT_RE = re.compile(r"(?m)#[^\n]*$")


def name_key(s: str) -> str:
    """A name as DPL may print it: HfTaskD0, hf-task-d0, hf_task_d0 -> hftaskd0
    (DPL's task names are the struct's, kebab-cased, abbreviations folded)."""
    return s.replace("-", "").replace("_", "").lower()


def vocabulary(repo: str, root: str, names: set, literals: set) -> None:
    """Add root's names (NAME_OWNERS only, by name_key) and string literals."""
    take_names = repo.startswith(NAME_OWNERS)
    for d, f in _walk(root):
        cmake = f == "CMakeLists.txt" or f.endswith(".cmake")
        if not (cmake or f.endswith(EXTS + SH_EXTS + PY_EXTS)):
            continue
        try:
            src = open(os.path.join(d, f), errors="replace").read()
        except OSError:
            continue
        if f.endswith(EXTS):
            src = _uncomment(src)
            lits = [m.group(1) for m in STR_RE.finditer(src)]
            code = STR_RE.sub(" ", src)
        else:
            src = _HASH_COMMENT_RE.sub("", src)
            lits = [a if a is not None else b for a, b in _PY_STR_RE.findall(src)]
            code = _PY_STR_RE.sub(" ", src)
        for x in lits:
            x = _unescape(x).strip()
            if 2 <= len(x) <= MAX_LITERAL and "\n" not in x:
                literals.add(x)
        if take_names:
            # CMake: the unquoted arguments too (o2physics_add_dpl_workflow(hf-task-d0 ...))
            for ident in set((re.findall(r"[A-Za-z][A-Za-z0-9_-]*", code) if cmake else _IDENT_RE.findall(code))):
                if len(ident) >= 2:
                    names.add(name_key(ident))


def head(root: str) -> str | None:
    r = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def build(sources: str, repos: list[str]) -> dict | None:
    """(Re)build <sources>/templates.json from <sources>/<owner>__<repo>; None if nothing changed."""
    heads = {}
    for r in repos:
        h = head(os.path.join(sources, r.replace("/", "__")))
        if h:
            heads[r] = h
    path = os.path.join(sources, INDEX)
    try:
        old = json.load(open(path))
        if old.get("version") == INDEX_VERSION and old.get("heads") == heads:
            return None
    except (OSError, ValueError):
        pass
    t0, templates, names, literals = time.time(), [], set(), set()
    for r in heads:
        root = os.path.join(sources, r.replace("/", "__"))
        templates += extract(r, root)
        vocabulary(r, root, names, literals)
    idx = {"version": INDEX_VERSION, "heads": heads, "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "seconds": round(time.time() - t0, 1), "templates": templates,
           "names": sorted(names), "literals": sorted(literals)}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(idx, f)
    os.replace(tmp, path)
    return idx


# ---------------------------------------------------------------- matching
SEV_RE = re.compile(r"^(?:.*?\[(?:FATAL|ERROR|ALARM|CRITICAL|WARN|WARNING|IMPORTANT|INFO)\]:?"
                    r"|.*?!!! (?:Error|Warning|Fatal|Alarm)\s*-)\s*")     # FairLogger/DPL; InfoLogger
BOOST_RE = re.compile(r'^(?P<file>\S+?): (?P<sev>(?:fatal )?error): in "(?P<case>[^"]+)": (?P<rest>.*)$')
BOOST_CHECK_RE = re.compile(r"^(?P<crit>critical )?check (?P<expr>.+?) has failed(?P<vals> \[.*)?$")
# The tools whose own messages end a build: their fixed text, the rest values.
# (name, pattern over the message, template)
TOOLS = [(n, re.compile(rx), t) for n, rx, t in [
    ("ninja", r"^ninja: build stopped: subcommand failed\.$", "ninja: build stopped: subcommand failed."),
    # The Hyperloop test runner's last line when o2 failed (a summary, see logscan.SUMMARY_RE).
    ("hyperloop", r"^ERROR: O2 exit code is (\d+)$", "ERROR: O2 exit code is _"),
    ("ninja", r"^ninja: build stopped: cannot make progress due to previous errors\.$",
     "ninja: build stopped: cannot make progress due to previous errors."),
    ("ninja", r"^ninja: error: (.+)$", "ninja: error: _"),
    ("make", r"^g?make(?:\[(?:\d+|N)\])?: \*\*\* Waiting for unfinished jobs\.*$", "make: *** Waiting for unfinished jobs"),
    ("make", r"^g?make(?:\[(?:\d+|N)\])?: \*\*\* \[(.*)\] Error (\S+)$", "make: *** [_] Error _"),
    ("make", r"^g?make(?:\[(?:\d+|N)\])?: \*\*\* (.+)$", "make: *** _"),
    ("ld", r"^collect2: error: ld returned (\S+) exit status$", "collect2: error: ld returned _ exit status"),
    ("ld", r"^(?:clang\+\+|clang|c\+\+|g\+\+|gcc|cc): error: linker command failed with exit code (\S+) "
           r"\(use -v to see invocation\)$", "error: linker command failed with exit code _ (use -v to see invocation)"),
    ("ctest", r"^(?:N|\d+)% tests passed, (?:N|\d+) tests failed out of (?:N|\d+)$",
     "_% tests passed, _ tests failed out of _"),
    ("autoconf", r"^(?:.*\s)?configure: error: (.+)$", "configure: error: _"),
    ("rootcling", r"^Error: \S*rootcling: compilation failure \((.*)\)$", "Error: rootcling: compilation failure (_)"),
    ("recc", r'^Retry limit \((.*?)\) exceeded for "(.*?)", last gRPC error was (.*)$',
     'Retry limit (_) exceeded for "_", last gRPC error was _'),
    ("recc", r'^Error while querying action cache at "(.*?)": (.*)$', 'Error while querying action cache at "_": _'),
    ("recc", r"^Failed to download output blob (.*?): (.*)$", "Failed to download output blob _: _"),
    ("recc", r"^Failed to download blob: (.*)$", "Failed to download blob: _"),
    ("recc", r"^Failed to copy file from (.*)$", "Failed to copy file from _"),
    ("recc", r"^Error remapping file: (.*)$", "Error remapping file: _"),
]]
CTEST_RE = re.compile(r"^Test: (?P<name>\S+?)(?= \*\*\*| Subprocess| Failed| \(| Exception|$)")
CTEST_PHRASES = ["***Failed", "Required regular expression not found.", "Subprocess aborted***Exception:",
                 "***Timeout", "***Exception: SegFault", "***Exception: Other", "Child aborted***Exception:",
                 "***Not Run", "Failed"]

# What changes between two occurrences of the same failure and says nothing
# about it. The rest of a filler (a CCDB path, a device name) is what tells two
# failures of one template apart: hashed with a key, never shown.
_FILL_RES = [
    (re.compile(r"0x[0-9a-fA-F]+"), "<ptr>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{7,64}\b"), "<hex>"),
    (re.compile(r"\b\d{4}-\d\d-\d\d(?:[T@ ]\d\d:\d\d(?::\d\d)?(?:\.\d+)?)?\S*"), "<time>"),
    (re.compile(r"\b\d\d:\d\d:\d\d(?:\.\d+)?\b"), "<time>"),
    (re.compile(r"((?:pid|PID|port|agent_id)[-_ :=@]*)\S*?\d+"), r"\1N"),
    (re.compile(r"(/(?:private/)?(?:var/folders|tmp)/)\S*|<tmp>"), "<tmp>"),
    (re.compile(r"\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"), "N"),
    (re.compile(r"\s+"), " "),
]


def normalise_filler(v: str) -> str:
    v = v.strip()
    for rx, repl in _FILL_RES:
        v = rx.sub(repl, v)
    return v


def message_of(sig_exact: str) -> str:
    first = sig_exact.split("\n", 1)[0]
    m = SEV_RE.match(first)
    if m:
        return first[m.end():].strip()
    i = first.find("Error in <")
    return first[i:] if i >= 0 else first.strip()


#: The kinds whose message may be printed by public code: compiler and runtime
#: errors, and aliBuild's own failed source updates (logscan "git-fetch",
#: printed by aliBuild's workarea.py).
SOURCE_KINDS = ("error", "git-fetch")


# ---------------------------------------------------------------- public fillers
# A filler is shown (fillers_public) when it cannot name a person, a machine or
# a private file, by one of these rules, else it stays a NULL (its hash is in
# filler_hash as before):
#   number   only digits and the punctuation between them: exit codes, signals,
#            sizes, positions, timestamps, run numbers.
#   literal  the whole value is a string literal of the public code.
#   path     a path (alien://, a public service's URL, or bare) that is a
#            production dataset (/alice/data, /alice/sim) or, its numeric parts
#            out, a string literal of the public code (a CCDB path).
#   names    every name in it (split at spaces and punctuation, "::" and "."
#            included) is a name of O2's code by name_key -- a device, task,
#            option, table or type name; "o2-analysis-" and "o2-" prefixes and
#            numeric suffixes ("_1") aside --, a number, or one of FIXED_WORDS.
#            A path never passes this way.
# Never, whatever the rule: a value after "pid" (it changes on every run and
# says nothing), after "user", "uid", "login", "account", "owner" or "email",
# holding "@", "~", a home or user directory, a letter outside ASCII, or a word
# that names one of the people who commit to or open pull requests on the
# indexed repositories (people(): their surnames, e-mail names, GitHub logins
# and the CERN-style logins made from them). Without that list (no commits or
# prs flow to read it from) only the number and dataset path rules apply.
FIXED_WORDS = {"sigsegv", "sigabrt", "sigkill", "sigterm", "sigbus", "sigfpe", "sigill", "sigint", "sigpipe",
               "killed", "segmentation", "fault", "aborted", "terminated", "error", "fatal", "warning", "info",
               "debug", "alarm", "critical", "important", "host", "ip", "tmp", "time", "ptr", "true", "false",
               "null", "nullptr", "nan", "inf", "yes", "no"}
_PID_CTX_RE = re.compile(r"(?i)\bpids?\W{0,3}$")
_PERSON_CTX_RE = re.compile(r"(?i)\b(?:user(?:name)?s?|uid|login|account|owner|e-?mail|author)\W{0,3}$")
_PERSON_RE = re.compile(r"(?i)[@~]|\busers?\b|/home/|/afs/|/eos/user|/cern\.ch/user/|\$HOME|[^\x00-\x7f]")
_NAME_SPLIT_RE = re.compile(r"[\s,;:()\[\]{}<>=\"'`|&*!?.+]+")
_NAME_AFFIX_RE = re.compile(r"^(?:o2-analysis-|o2-)|(?:[-_:]\d+)+$")
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?%?(?:[\s,;:/()\[\]+-]+[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?%?)*"
                        r"(?:\s*\(?(?:bytes?|[kKMG]i?B|s|ms|us|ns|MB/s)\)?)?")
_URL_RE = re.compile(r"^(?:alien://|file://|https?://(?:alice-ccdb|alimonitor|alien|ccdb-test)\.cern\.ch(?::\d+)?)")
_DATASET_RE = re.compile(r"^/alice/(?:data|sim)/\d{4}/")
_PATH_NUMERIC_RE = re.compile(r"^(?:\d+|\d+[-_.]\d+.*|[0-9a-f-]{32,36})$")
_WORD_RE = re.compile(r"[a-z]+")
MAX_FILLER = 300
FILLER_CTX = 24     # characters of the message before a filler, to read what it is


class Templates:
    """The index and the trees it was built from; match() a signature's text."""

    def __init__(self, sources: str, key: bytes | None = None):
        self.sources = sources
        idx = json.load(open(os.path.join(sources, INDEX)))
        self.heads: dict[str, str] = idx["heads"]
        self.t: list[dict] = idx["templates"]
        self.names: set[str] = set(idx.get("names", ()))
        self.people: set[str] | None = None     # set by the publisher: people()
        self.rules: dict[str, int] = {}          # fillers seen, by the rule that showed or withheld them
        self.literals: set[str] = set(idx.get("literals", ()))
        self.key = key
        self.by_key: dict[str, list[int]] = {}
        self.rx: dict[int, re.Pattern] = {}
        for i, t in enumerate(self.t):
            anchor = max((x for x in t["pieces"] if x not in SPECIAL), key=lambda x: len(x.strip())).strip()
            t["anchor"] = anchor
            self.by_key.setdefault(anchor[:MIN_ANCHOR], []).append(i)
        self._cmake: str | None = None

    # -- source templates
    def _regex(self, i: int) -> re.Pattern:
        if i not in self.rx:
            parts = []
            for p in self.t[i]["pieces"]:
                if p == HOLE:
                    parts.append(r"(.*?)")
                elif p == CLS:
                    parts.append(r"(?:[\w:]*::)?")
                else:
                    parts.append(r"\s*".join(re.escape(w) for w in re.split(r"\s+", p.strip())) or r"\s*")
            self.rx[i] = re.compile(r"^\s*" + r"\s*".join(parts) + r"\s*$", re.S)
        return self.rx[i]

    def _source(self, msg: str):
        cands = set()
        for k in range(0, max(0, len(msg) - MIN_ANCHOR) + 1):
            for i in self.by_key.get(msg[k:k + MIN_ANCHOR], ()):
                if self.t[i]["anchor"] in msg:
                    cands.add(i)
        best = None
        for i in sorted(cands):
            if self._regex(i).match(msg):
                lit = sum(len(x.strip()) for x in self.t[i]["pieces"] if x not in SPECIAL)
                if best is None or lit > best[1]:
                    best = (i, lit)
        if best is None:
            return None
        t = self.t[best[0]]
        text = "".join("_" if x == HOLE else "" if x == CLS else x for x in t["pieces"]).strip()
        return (text, t["repo"], t["path"], t["line"], *_groups(self._regex(best[0]).match(msg), msg))

    # -- tests
    def _find(self, rel: str):
        rel = rel.split(":", 1)[-1].lstrip("/")
        for repo in self.heads:
            root = os.path.join(self.sources, repo.replace("/", "__"))
            for cand in (rel, rel.split("/", 1)[-1]):
                p = os.path.join(root, cand)
                if os.path.isfile(p):
                    return repo, cand, p
        return None

    def _cmake_text(self) -> str:
        if self._cmake is None:
            parts = []
            for repo in self.heads:
                for d, f in _walk(os.path.join(self.sources, repo.replace("/", "__"))):
                    if f == "CMakeLists.txt" or f.endswith(".cmake"):
                        parts.append(open(os.path.join(d, f), errors="replace").read())
            self._cmake = "\n".join(parts)
        return self._cmake

    def _test(self, first: str):
        m = BOOST_RE.match(first)
        if m:
            hit = self._find(m.group("file"))
            if not hit:
                return None
            raw = open(hit[2], errors="replace").read()
            flat, case, sev = re.sub(r"\s+", " ", raw), m.group("case"), m.group("sev")
            # "suite/case/_9": every named component must be in the file
            names = [c for c in case.split("/") if c and not re.fullmatch(r"_\d+", c)]
            if not names or not all(re.search(r"\b" + re.escape(c) + r"\b", flat) for c in names):
                return None
            lines = raw.splitlines()
            line = next((i + 1 for i, l in enumerate(lines) if re.search(r"\b" + re.escape(names[-1]) + r"\b", l)), None)
            c = BOOST_CHECK_RE.match(m.group("rest"))
            if not c:       # difference{...} between ..., a message: the case, the rest a value
                return f'{sev}: in "{case}": _', hit[0], hit[1], line, [m.group("rest")], [""]
            expr, vals, crit = c.group("expr"), (c.group("vals") or "").strip(), c.group("crit") or ""
            if expr in flat:
                line = next((i + 1 for i, l in enumerate(lines) if expr in re.sub(r"\s+", " ", l)), line)
                text = f'{sev}: in "{case}": {crit}check {expr} has failed' + (" [_]" if vals else "")
                return text, hit[0], hit[1], line, [vals[1:-1]] if vals else [], [""] if vals else []
            # The case is there but not this check: the test changed since the
            # log (the index is the branch head). The expression stays a value.
            text = f'{sev}: in "{case}": {crit}check _ has failed' + (" [_]" if vals else "")
            return text, hit[0], hit[1], line, [expr] + ([vals[1:-1]] if vals else []), [""] * (2 if vals else 1)
        m = CTEST_RE.match(first)
        if m:
            name = m.group("name")
            hit = self._find(name)
            if not hit and name not in self._cmake_text():
                return None
            rest = first[m.end():].strip()
            kept = [p for p in CTEST_PHRASES if p in rest]
            kept = [p for p in kept if not any(p != q and p in q for q in kept)]
            left = rest
            for p in kept:
                left = left.replace(p, " ")
            text = f"Test: {name} " + (" ".join(kept) if kept else "***Failed")
            return text, hit[0] if hit else None, hit[1] if hit else None, None, [left.strip()], [""]
        return None

    # -- the result
    def permalink(self, repo: str | None, path: str | None, line: int | None) -> str | None:
        if not repo or not path or repo not in self.heads:
            return None
        return f"https://github.com/{repo}/blob/{self.heads[repo]}/{path}" + (f"#L{line}" if line else "")

    def filler_hash(self, template_id: str, fillers: list[str]) -> str | None:
        if not self.key:
            return None     # never a plain hash: a short filler could be confirmed by guessing
        msg = "\x1f".join([template_id] + [normalise_filler(f) for f in fillers]).encode()
        return hmac.new(self.key, msg, hashlib.sha256).hexdigest()[:16]

    def match(self, kind: str, sig_exact: str) -> dict | None:
        """{fingerprint, fingerprint_kind, fingerprint_src, filler_hash} for a signature, or None."""
        first = sig_exact.split("\n", 1)[0]
        r, fk = None, None
        if kind in ("error", "test"):
            r = self._test(first)
            fk = "test" if r else None
        if r is None and kind in ("error", "test", "remote-cache"):
            msg = message_of(sig_exact)
            for _name, rx, text in TOOLS:
                t = rx.match(msg)
                if t:
                    r, fk = (text, None, None, None, *_groups(t, msg)), "tool"
                    break
        if r is None and kind in SOURCE_KINDS:
            r = self._source(message_of(sig_exact))
            fk = "source" if r else None
            # A wrapper ("Exception while running: _. Rethrowing.", "...device
            # shutting down. Reason: _") whose last value is itself a message
            # with a template: that message is the error, the wrapper only
            # carries it. Two levels at most; the inner match is anchored on
            # the whole value, as every match is.
            for _ in range(2):
                # (its own severity prefix out: "- First error: [ERROR] ...")
                inner = next((self._source(message_of(f)) for f in reversed(r[4]) if len(f.strip()) >= MIN_LITERAL),
                             None) if r else None
                if not inner:
                    break
                r = inner
        if r is None:
            return None
        text, repo, path, line, fillers, ctx = r
        tid = template_id(text)
        return {"fingerprint": text, "fingerprint_kind": fk, "fingerprint_src": self.permalink(repo, path, line),
                "filler_hash": self.filler_hash(tid, fillers) if any(f.strip() for f in fillers) else None,
                "fillers_public": self.fillers_public(fillers, ctx)}

    # -- the values that may be shown
    def public_filler(self, v: str, before: str = "") -> tuple[str | None, str]:
        """(v, rule) when v may be published (see FIXED_WORDS above), else (None, why)."""
        v = v.strip()
        if not v:
            return None, "empty"
        if _PID_CTX_RE.search(before):
            return None, "pid"
        if _PERSON_CTX_RE.search(before) or _PERSON_RE.search(v):
            return None, "person"
        if len(v) > MAX_FILLER:
            return None, "long"
        if _NUMBER_RE.fullmatch(v):
            return v, "number"
        if self.people is None:
            return (v, "path") if _DATASET_RE.match(_URL_RE.sub("", v)) else (None, "no people list")
        if any(w in self.people for w in _WORD_RE.findall(v.lower())):
            return None, "person"
        core = v.strip("'\"`")
        if core in self.literals:
            return v, "literal"
        if "/" in core or "\\" in core:
            return (v, "path") if self._public_path(core) else (None, "path")
        toks = [x for x in _NAME_SPLIT_RE.split(core) if x]
        if toks and all(self._name(x) for x in toks):
            return v, "names"
        return None, "names"

    def _name(self, tok: str) -> bool:
        if _NUMBER_RE.fullmatch(tok) or tok.lower() in FIXED_WORDS:
            return True
        k = name_key(tok)
        return k in self.names or name_key(_NAME_AFFIX_RE.sub("", tok)) in self.names

    def _public_path(self, p: str) -> bool:
        p = _URL_RE.sub("", p.split("?", 1)[0]).strip()
        if _DATASET_RE.match(p):
            return True
        if p.startswith("./"):
            p = p[2:]
        segs = [x for x in p.strip("/").split("/") if x and not _PATH_NUMERIC_RE.match(x)]
        return bool(segs) and ("/".join(segs) in self.literals or "/" + "/".join(segs) in self.literals)

    def fillers_public(self, fillers: list[str], ctx: list[str] | None = None) -> str | None:
        """The fillers as a JSON list, each the value or null (withheld); None
        when there are none or none may be shown."""
        ctx = ctx or [""] * len(fillers)
        out = []
        for f, c in zip(fillers, ctx):
            # A whole log line carried as a value ("- First error: <time>[FATAL] ...",
            # its message without a template): its time and severity prefix out.
            if SEV_RE.match(f):
                f = message_of(f)
            v, rule = self.public_filler(f, c)
            k = f"{'shown' if v is not None else 'withheld'} {rule}"
            self.rules[k] = self.rules.get(k, 0) + 1
            out.append(v)
        return json.dumps(out) if any(x is not None for x in out) else None


def people(*dbs) -> set[str]:
    """The words that name people, from the indexed commits (author names and
    e-mail names) and pull requests (GitHub logins), lower case, ASCII: each
    surname, e-mail name part and login part (3 letters or more), and the
    CERN-style logins made of an initial and the surname (8 letters at most)."""
    import sqlite3
    import unicodedata
    fold = lambda x: unicodedata.normalize("NFKD", x).encode("ascii", "ignore").decode().lower()
    names, logins = set(), set()
    for db in dbs:
        if not db or not os.path.exists(db):
            continue
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            tabs = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "commits" in tabs:
                for n, e in c.execute("SELECT DISTINCT author_name, author_email FROM commits"):
                    names.add(fold(n or ""))
                    logins.add(fold((e or "").split("@", 1)[0]))
            if "prs" in tabs:
                logins.update(fold(a) for (a,) in c.execute("SELECT DISTINCT author FROM prs") if a)
        finally:
            c.close()
    out = set()
    for n in names:
        ws = _WORD_RE.findall(n)
        if len(ws) >= 2:
            out.add(ws[-1])
            out.add((ws[0][0] + "".join(ws[1:]))[:8])
            out.add((ws[0][0] + ws[-1])[:8])
        out.update(w for w in ws[1:])
    for l in logins:
        out.add(re.sub(r"[^a-z]", "", l))
        out.update(_WORD_RE.findall(l))
    return {w for w in out if len(w) >= 3} - FIXED_WORDS


def _groups(m: re.Match, msg: str) -> tuple[list[str], list[str]]:
    """A match's groups (the fillers), and the text of msg just before each."""
    n = len(m.groups())
    return (list(m.groups()),
            [msg[max(0, m.start(i) - FILLER_CTX):max(0, m.start(i))] if m.start(i) >= 0 else "" for i in range(1, n + 1)])


def template_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def load(sources: str | None, key: str | None = None) -> Templates | None:
    """The index under sources, or None when there is none (no readable templates then)."""
    if not sources or not os.path.exists(os.path.join(sources, INDEX)):
        return None
    return Templates(sources, key.encode() if key else None)


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] != "build":
        sys.exit(__doc__)
    idx = build(sys.argv[2], [a.split("=", 1)[0] for a in sys.argv[3:]])
    if idx is None:
        print("srctemplates: heads unchanged, index kept", file=sys.stderr)
    else:
        by: dict[str, int] = {}
        for t in idx["templates"]:
            by[t["repo"]] = by.get(t["repo"], 0) + 1
        print(f"srctemplates: {len(idx['templates'])} templates in {idx['seconds']}s: {by}", file=sys.stderr)
