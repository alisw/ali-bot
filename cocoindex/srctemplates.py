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
INDEX_VERSION = 3   # 2: signposts, ROOT-named calls read both ways; 3: shell, Python, ROOT class prefix

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
FMT_RE = re.compile(r"%\{[^{}]*\}[A-Za-z]|\{[^{}]*\}|%[-+ #0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?(?:hh|h|ll|l|L|q|j|z|t)?[diouxXeEfFgGaAcspn]")
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
PY_CALL_RE = re.compile(r"\b(?:print|raise\s+[\w.]+|(?:logging|logger|log|self\.\w*log\w*)\.\w+"
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
                pcs = _keep(_sh_pieces(m.group(1), src[m.start():m.start() + 6] == "printf"))
                if pcs:
                    out.append({"repo": repo, "path": rel, "line": src.count("\n", 0, m.start()) + 1, "pieces": pcs})
            continue
        if f.endswith(PY_EXTS):
            for m in PY_CALL_RE.finditer(src):
                args = _py_args(src, m.end())
                pcs = _keep(_py_pieces(args)) if args is not None else None
                if pcs:
                    out.append({"repo": repo, "path": rel, "line": src.count("\n", 0, m.start()) + 1, "pieces": pcs})
            continue
        for m in CALL_RE.finditer(src):
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
            for pcs in map(_keep, variants):
                if pcs:
                    out.append({"repo": repo, "path": rel, "line": line, "pieces": pcs})
    return out


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
    t0, templates = time.time(), []
    for r in heads:
        templates += extract(r, os.path.join(sources, r.replace("/", "__")))
    idx = {"version": INDEX_VERSION, "heads": heads, "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "seconds": round(time.time() - t0, 1), "templates": templates}
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


class Templates:
    """The index and the trees it was built from; match() a signature's text."""

    def __init__(self, sources: str, key: bytes | None = None):
        self.sources = sources
        idx = json.load(open(os.path.join(sources, INDEX)))
        self.heads: dict[str, str] = idx["heads"]
        self.t: list[dict] = idx["templates"]
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
        return text, t["repo"], t["path"], t["line"], list(self._regex(best[0]).match(msg).groups())

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
                return f'{sev}: in "{case}": _', hit[0], hit[1], line, [m.group("rest")]
            expr, vals, crit = c.group("expr"), (c.group("vals") or "").strip(), c.group("crit") or ""
            if expr in flat:
                line = next((i + 1 for i, l in enumerate(lines) if expr in re.sub(r"\s+", " ", l)), line)
                text = f'{sev}: in "{case}": {crit}check {expr} has failed' + (" [_]" if vals else "")
                return text, hit[0], hit[1], line, [vals[1:-1]] if vals else []
            # The case is there but not this check: the test changed since the
            # log (the index is the branch head). The expression stays a value.
            text = f'{sev}: in "{case}": {crit}check _ has failed' + (" [_]" if vals else "")
            return text, hit[0], hit[1], line, [expr] + ([vals[1:-1]] if vals else [])
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
            return text, hit[0] if hit else None, hit[1] if hit else None, None, [left.strip()]
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
        if r is None and kind in ("error", "test"):
            msg = message_of(sig_exact)
            for _name, rx, text in TOOLS:
                t = rx.match(msg)
                if t:
                    r, fk = (text, None, None, None, list(t.groups())), "tool"
                    break
        if r is None and kind == "error":
            r = self._source(message_of(sig_exact))
            fk = "source" if r else None
        if r is None:
            return None
        text, repo, path, line, fillers = r
        tid = template_id(text)
        return {"fingerprint": text, "fingerprint_kind": fk, "fingerprint_src": self.permalink(repo, path, line),
                "filler_hash": self.filler_hash(tid, fillers) if any(f.strip() for f in fillers) else None}


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
