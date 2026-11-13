#!/usr/bin/env python3

"""Diagnose the CI checkers: what the workers are doing, and why a build failed.

This exists because answering "why did that check go red?" by hand costs an
absurd amount of reading. A slc10 worker's stderr is a quarter of a million
lines; a single grep for "error" over one of them returned 657 KB, and one
aliBuild DEBUG line -- a package's full recipe, inlined -- is itself several
thousand characters. Nothing about that is hard, it is just enormous, and the
enormity is the whole problem.

So the rule every tool here follows is: NEVER RETURN RAW LOG. Fetch as much as
it takes, return a bounded, deduplicated summary. A tool that can emit an
unbounded blob is a tool that will one day emit a quarter of a million lines.

Read-only by construction. It resolves the read-only Nomad gate token and the
GitHub gate token from the security-proxy agent socket at call time -- the same
ones ~/.zsh-customizations/nomad.zsh exports -- and never the attended nomad-rw
slot. There is deliberately no way to dispatch, stop, or write a status from
here: those are decisions a human makes, and a diagnostic tool that can also
change the cluster is no longer a diagnostic tool.
"""

import bisect
import json
from datetime import datetime
import os
import os.path
import re
import subprocess
import sys

from collections import Counter, defaultdict

import requests

from mcp.server.fastmcp import FastMCP

AGENT_SOCKET = "/usr/local/var/run/security-proxy/agent/agent.sock"
ALIBOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# How much of a log to pull before giving up, and how much to ever hand back.
# The fetch cap is generous because the server can afford the bytes; the return
# caps are mean because the reader cannot.
MAX_FETCH_BYTES = 64 * 1024 * 1024
MAX_LINE_CHARS = 300
MAX_RETURN_LINES = 60

mcp = FastMCP("ci")


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


def _load_error_patterns():
    """Lift the `NAME_RE = re.compile("...")` assignments out of the source.

    Read rather than imported. Importing report-pr-errors means executing it,
    which pulls in alibot_helpers and its whole dependency tree -- so this
    server would need ali-bot installed in its venv just to learn what an error
    looks like, and would fall back to a worse pattern set the day that broke.

    Only string literals passed to re.compile are evaluated, and only for the
    names listed above, so this reads the file without running any of it.
    """
    import ast
    path = os.path.join(ALIBOT, "report-pr-errors")
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
            found[WANTED_PATTERNS[target.id]] = re.compile(
                ast.literal_eval(call.args[0]))
        except (ValueError, re.error):
            continue          # a pattern we cannot read is one we skip, not a crash
    if "error" not in found:
        raise RuntimeError("ERRORS_RE not found in %s" % path)
    # Re-key in WANTED_PATTERNS order. `found` was filled in the order the
    # assignments appear in report-pr-errors, which puts the broad ERRORS_RE
    # first -- so without this the specific kinds are unreachable no matter how
    # this table is written.
    return {kind: found[kind] for kind in WANTED_PATTERNS.values()
            if kind in found}


try:
    PATTERNS = _load_error_patterns()
except Exception as err:                       # noqa: BLE001
    print("could not import report-pr-errors patterns: %s" % err, file=sys.stderr)
    PATTERNS = {"error": re.compile(r": (fatal )?error:|^ERROR: |ninja: build stopped")}


# ---------------------------------------------------------------------------
# Talking to the cluster, through the broker
# ---------------------------------------------------------------------------

def _gate(service):
    """(base_url, token) for a security-proxy route, resolved at call time.

    Both rotate -- the port is random per proxy start and the token rotates
    daily -- so nothing here may be cached across calls.
    """
    def ask(*args):
        return subprocess.run(
            ["security-proxy-token", "--socket", AGENT_SOCKET, *args],
            capture_output=True, text=True, check=True).stdout.strip()
    return ask("--addr"), ask(service)


def nomad_get(path, **params):
    addr, token = _gate("nomad")
    reply = requests.get(addr + path, params=params, timeout=60,
                         headers={"X-Nomad-Token": token})
    reply.raise_for_status()
    return reply.json()


def nomad_log(alloc_id, task, stream="stderr", max_bytes=MAX_FETCH_BYTES):
    """The tail of one task's log. Returns (text, truncated).

    origin=end means Nomad seeks backwards from the end, so a running build's
    most recent output costs one request rather than a stream of everything it
    has ever printed.

    `truncated` says the window filled up and older output was NOT read. It has
    to be reported: a claim worker writes hundreds of MB a day, so a failure
    from a few hours ago falls outside the default window, and a search that
    silently answers "no match" reads as "it never happened". That has produced
    confident wrong answers more than once.
    """
    addr, token = _gate("nomad")
    reply = requests.get(
        "%s/v1/client/fs/logs/%s" % (addr, alloc_id),
        params={"task": task, "type": stream, "plain": "true",
                "origin": "end", "offset": max_bytes, "follow": "false"},
        headers={"X-Nomad-Token": token}, timeout=300)
    reply.raise_for_status()
    text = reply.text
    truncated = len(reply.content) >= max_bytes - 65536
    if truncated:
        # The window opens mid-line; drop the fragment so a half-eaten
        # timestamp cannot parse as a real one.
        text = text.split("\n", 1)[-1]
    return text, truncated


#: aliBuild stamps the lines it emits itself with LOCAL time, and so do
#: claim-builder.sh's banners and heartbeats -- on stderr as the `set -x` trace
#: of the printf, whose first argument is the date:
#:   2026-08-21@14:10:21:DEBUG:O2Suite:autotools:slc10_x86-64-...:
#:   2026-11-13@18:23:40 ===== ROUND 53 BEGIN check=... =====
#:   + printf '%s ===== ROUND %s END pr=%s result=claim-lost =====\n' 2026-11-13@18:23:40 53 18728
STAMP_RE = re.compile(r"^(?:\++ printf '[^']*' )?(\d{4}-\d\d-\d\d)@(\d\d:\d\d:\d\d)\b")


def line_time(line):
    """('2026-08-21', '14:10:21') for a stamped line, else None."""
    found = STAMP_RE.match(line)
    return (found.group(1), found.group(2)) if found else None


#: No timezone suffix on purpose: the log has none, and a UTC value from a
#: GitHub status would silently compare an hour or two early.
SINCE_RE = re.compile(r"^(?:(\d{4}-\d\d-\d\d)[@T ]+)?(\d{1,2}:\d\d(?::\d\d)?)$")


def parse_since(since, lines):
    """Normalise a `since` argument to a (date, time) pair, or None.

    Accepts "14:10", "14:10:21", or a date before it: "2026-08-21 14:10",
    "2026-08-21@14:10:21", "2026-08-21T14:10". A bare time means today IN THE
    LOG'S OWN CLOCK -- the date of its newest stamped line, not this Mac's --
    because the builders are on CEST and this machine need not be.

    Anything else is refused rather than guessed at: "2026-11-13 15:20" used to
    be read as a bare time, so the label said "since 2026-11-12 2026-11-13
    15:20:00" and no line ever compared after it.
    """
    if not since:
        return None
    found = SINCE_RE.match(since.strip())
    if not found:
        raise ValueError("since=%r: expected HH:MM[:SS] or YYYY-MM-DD HH:MM[:SS] "
                         "in the builder's local clock" % since)
    date, clock = found.groups()
    parts = (clock.split(":") + ["00"])[:3]
    clock = ":".join(part.zfill(2) for part in parts)
    if date is None:
        date = ""
        for raw in reversed(lines):
            stamp = line_time(raw)
            if stamp:
                date = stamp[0]
                break
    return (date, clock)


def window_from(lines, since):
    """Index of the first line at or after `since`.

    The log is chronological, so `since` selects a suffix and this is a scan for
    the boundary rather than a filter. Unstamped output -- raw compiler and
    shell text, which is most of a build log -- belongs to the last stamped line
    before it, so slicing at the boundary keeps it with the right round.
    """
    if since is None:
        return 0
    for index, raw in enumerate(lines):
        stamp = line_time(raw)
        if stamp and stamp >= since:
            return index
    return len(lines)


#: build-loop.sh prints one of these blocks per round, after merging the PR:
#:
#:   ci-round: check=build/O2/... pr=alisw/alidist#6193 head=09d34327...
#:   ci-round:   alidist=1a2b3c4d...
#:   ci-round:   O2=5e6f7a8b...
#:
#: It is the only thing in the log that says WHAT a stretch of output was
#: building. Everything else -- timestamps, PR numbers in passing, pinned SHAs
#: quoted by aliBuild -- has to be inferred, and inference has been wrong.
ROUND_RE = re.compile(r"^ci-round: check=(\S+) pr=(\S+) head=(\S+)")
ROUND_PKG_RE = re.compile(r"^ci-round:\s+(\S+)=(\S+)")


def find_rounds(lines):
    """[(index, check, pr, head, {pkg: sha}), ...] for the rounds in `lines`."""
    rounds = []
    for index, raw in enumerate(lines):
        head = ROUND_RE.match(raw)
        if head:
            rounds.append([index, head.group(1), head.group(2),
                           head.group(3), {}])
        elif rounds:
            pkg = ROUND_PKG_RE.match(raw)
            # Only while still in the block: a later ci-round: line starts a new
            # one. Not "the next line": `set -x` traces the loop that prints the
            # block, a dozen lines per checkout, some of them multi-line.
            if pkg and index - rounds[-1][0] < 200:
                rounds[-1][4][pkg.group(1)] = pkg.group(2)
    return rounds


#: claim-builder.sh brackets every claim ATTEMPT with a pair of banners, on
#: stdout as text and on stderr as the `set -x` trace of the printf:
#:
#:   2026-11-13@18:23:40 ===== ROUND 53 BEGIN check=build/O2Physics/staging pr=18737 sha=088646b4 type=failed =====
#:   2026-11-13@18:28:11 ===== ROUND 53 END pr=18737 result=built rc=0 duration=4m (271s) =====
#:   + printf '%s ===== ROUND %s END pr=%s result=%s =====\n' 2026-11-13@18:23:40 53 18737 stale-skip
#:
#: Unlike the ci-round: block these are printed whether or not anything gets
#: built, so they are the only boundaries that see a lost claim, a stale-skip, a
#: status-cap skip or a merge conflict. "ROUND n" is the claim-builder's pass
#: counter: one pass walks the queue, so several attempts share an n, and it
#: restarts at 1 with the task.
CLAIM_BEGIN_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d)@(\d\d:\d\d:\d\d) ===== ROUND (\d+) BEGIN check=(\S+) "
    r"pr=(\S+) sha=(\S+) type=(\S+) =====")
CLAIM_END_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d)@(\d\d:\d\d:\d\d) ===== ROUND (\d+) END pr=(\S+) "
    r"result=(\S+)(?: rc=(\d+))?(?: duration=\S+ \((\d+)s\))? =====")
XTRACE_PRINTF_RE = re.compile(r"^\++ printf '([^']*)\\n' (.*)$")
#: build-loop.sh's give-up when a commit cannot take another status. It exits 0,
#: so the END banner says "built rc=0" for a round that built nothing.
STATUS_CAP_RE = re.compile(r"^build-loop: .* has reached GitHub's status limit "
                           r"for .*skipping this build")


def banner_text(raw):
    """A ROUND banner as printed, whether `raw` is the text or its xtrace."""
    found = XTRACE_PRINTF_RE.match(raw)
    if not found:
        return raw
    pieces = re.split(r"%(?:\.\d+)?s", found.group(1))
    args = [arg.strip("'") for arg in found.group(2).split()]
    if len(pieces) != len(args) + 1:
        return raw
    return pieces[0] + "".join(arg + piece for arg, piece in zip(args, pieces[1:]))


def find_claims(lines):
    """[{...}, ...]: claim-builder.sh's ROUND BEGIN/END pairs, in log order.

    Paired by position, not by number, because n is shared within a pass. An
    attempt with no END ("stop" None) is either still running or was killed.
    """
    claims = []
    for index, raw in enumerate(lines):
        if "===== ROUND " not in raw:          # cheap: a log is ~250k lines
            continue
        text = banner_text(raw)
        found = CLAIM_BEGIN_RE.match(text)
        last = claims[-1] if claims else None
        if found:
            when = found.group(1, 2)
            # Both streams merged into one log would show every banner twice.
            if last and last["stop"] is None and last["when"] == when and \
               (last["n"], last["check"], last["pr"]) == found.group(3, 4, 5):
                continue
            claims.append({"start": index, "stop": None, "when": when,
                           "n": found.group(3), "check": found.group(4),
                           "pr": found.group(5), "sha": found.group(6),
                           "type": found.group(7), "result": None, "rc": None,
                           "seconds": None, "status_cap": False})
            continue
        found = CLAIM_END_RE.match(text)
        if found and last and last["stop"] is None and \
           (last["n"], last["pr"]) == found.group(3, 4):
            last.update(stop=index, result=found.group(5), rc=found.group(6),
                        seconds=found.group(7) and int(found.group(7)))
    for claim in claims:
        if claim["rc"] == "0":
            claim["status_cap"] = any(STATUS_CAP_RE.match(raw) for raw
                                      in lines[claim["start"]:claim["stop"]])
    return claims


def claim_class(claim, last):
    """What a claim attempt came to, in one word (plus the rc of a failure)."""
    if claim["result"] is None:
        # The last one has not finished; an earlier one never will: the worker
        # was killed or restarted mid-round.
        return "running" if last else "no-END"
    if claim["result"] != "built":
        return claim["result"]       # claim-lost, stale-skip, closed, not-head
    if claim["rc"] == "0":
        return "status-cap" if claim["status_cap"] else "ok"
    if claim["rc"] == "4":
        return "verdict-only"        # build-loop.sh: merge conflict or too big
    return "FAILED rc=%s" % claim["rc"]


def held_claims(claims):
    """Positions of the claims that were not lost: the numbered rounds.

    A lost claim is a few dozen lines of nothing, and a busy worker loses
    several a minute; numbering them would shift every round= between a
    ci_rounds call and the ci_build_errors that follows it.
    """
    return [position for position, claim in enumerate(claims)
            if claim["result"] != "claim-lost"]


def claim_stop(claims, position, lines):
    """One past the last line of claims[position]: its END, else the next BEGIN."""
    claim = claims[position]
    if claim["stop"] is not None:
        return claim["stop"] + 1
    return claims[position + 1]["start"] if position + 1 < len(claims) else len(lines)


def attach_provenance(claims, lines):
    """Give each claim the ci-round: block build-loop.sh printed inside it."""
    rounds = find_rounds(lines)
    starts = [entry[0] for entry in rounds]
    for position, claim in enumerate(claims):
        slot = bisect.bisect_left(starts, claim["start"])
        inside = slot < len(rounds) and starts[slot] < claim_stop(claims, position, lines)
        claim["ci_round"] = rounds[slot] if inside else None


def describe_claim(claim):
    if claim.get("ci_round"):
        return describe_round(claim["ci_round"])
    return "%s on pr=%s@%.8s" % (claim["check"], claim["pr"], claim["sha"])


PR_OK_RE = re.compile(r"^\+*\s*PR_OK=([01])\s*$")


def stamp_index(lines):
    """(positions, stamps): every stamped line, in order, for bisecting.

    Built once per call rather than scanned per round. A round banner and the
    `set -x` traces around it carry no timestamp, and the gap to the first
    aliBuild line is not small -- measured at 1734 lines on a real worker, where
    the checkout and merge output sits. Anything with a fixed look-ahead gets
    this wrong silently.
    """
    positions, stamps = [], []
    for index, raw in enumerate(lines):
        found = line_time(raw)
        if found:
            positions.append(index)
            stamps.append(found)
    return positions, stamps


def stamp_from(index, at, forward, limit=None):
    """The nearest stamp at or after `at` (or at or before it), else None.

    `limit` bounds the search to the caller's own region: without it a round
    that produced no stamped output at all -- a skipped check reports in well
    under a second -- borrows a time from its neighbour, which would put a
    plausible but entirely invented duration on it.
    """
    positions, stamps = index
    if forward:
        slot = bisect.bisect_left(positions, at)
        if slot >= len(stamps) or (limit is not None and positions[slot] >= limit):
            return None
        return stamps[slot]
    slot = bisect.bisect_right(positions, at) - 1
    if slot < 0 or (limit is not None and positions[slot] < limit):
        return None
    return stamps[slot]


def round_outcome(lines, start, stop, index):
    """('ok'|'FAILED'|None, seconds|None) for the round in lines[start:stop].

    build-loop.sh sets PR_OK=1 after reporting success and PR_OK=0 on the
    failure branch; `set -x` puts exactly one of them in the log per round,
    after the build and before the next banner. None means no verdict was
    reached -- still building if this is the last round, killed if it is not.
    """
    verdict = end = None
    for position in range(start, stop):
        found = PR_OK_RE.match(lines[position].strip())
        if found:
            verdict = "ok" if found.group(1) == "1" else "FAILED"
            end = position
            break
    began = stamp_from(index, start, forward=True, limit=stop)
    ended = (stamp_from(index, end, forward=False, limit=start)
             if end is not None else None)
    seconds = None
    if began and ended:
        try:
            seconds = (datetime.strptime("%s %s" % ended, "%Y-%m-%d %H:%M:%S")
                       - datetime.strptime("%s %s" % began, "%Y-%m-%d %H:%M:%S")
                       ).total_seconds()
        except ValueError:
            seconds = None
    # A window that starts mid-round clips the beginning, so the arithmetic can
    # still come out negative. Report nothing rather than a wrong number.
    if seconds is not None and seconds < 0:
        seconds = None
    return verdict, seconds


def human_duration(seconds):
    """"1h12m" / "22m03s" / "9s"; "" when it could not be worked out."""
    if seconds is None:
        return ""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return "%dh%02dm" % (hours, minutes)
    if minutes:
        return "%dm%02ds" % (minutes, secs)
    return "%ds" % secs


def describe_round(entry):
    _, check, pr, head, packages = entry
    parts = ["%s on %s@%.12s" % (check, pr, head)]
    parts += ["%s@%.12s" % item for item in sorted(packages.items())]
    return "; ".join(parts)


def log_span(lines):
    """What the window actually covers, dated. "" if nothing is stamped.

    The date is not decoration: a long-lived worker's log routinely spans
    midnight, and a bare "14:31:00 to 17:15:47" from yesterday is
    indistinguishable from the same clock times today. That has been read the
    wrong way round more than once.
    """
    first = last = None
    for raw in lines:
        stamp = line_time(raw)
        if stamp:
            first = first or stamp
            last = stamp
    if not first:
        return ""
    if first[0] == last[0]:
        return "%s %s to %s" % (first[0], first[1], last[1])
    return "%s %s to %s %s" % (first[0], first[1], last[0], last[1])


def scope_lines(lines, since, round_):
    """Slice a log to one round, or to a time. Returns (lines, start, label).

    An explicit `round_` wins over `since`: it is exact where a timestamp is a
    guess. The default "last" does not -- it would silently discard the `since`
    the caller did pass.

    Rounds are claim-builder.sh's ROUND BEGIN/END pairs that held their claim,
    numbered as ci_rounds numbers them. The ci-round: banners are only the
    fallback for logs without them: they mark just a merged build, so on a claim
    worker everything after one (lost claims, skips, the next PR's merge) was
    read as part of it.
    """
    claims = find_claims(lines) if round_ and round_ != "all" else []
    held = held_claims(claims)
    if held and not (since and round_ == "last"):
        try:
            number = len(held) if round_ == "last" else int(round_)
            if not 0 < number <= len(held):
                raise IndexError(round_)
        except (ValueError, IndexError):
            return lines, 0, ("round=%s not found; %d round(s) in the window"
                              % (round_, len(held)))
        index = held[number - 1]
        attach_provenance(claims, lines)
        begin, stop = claims[index]["start"], claim_stop(claims, index, lines)
        return lines[begin:stop], begin, "round=%d: %s" % (
            number, describe_claim(claims[index]))
    if round_ and round_ != "all" and not (since and round_ == "last"):
        rounds = find_rounds(lines)
        if not rounds:
            note = ("no ci-round: banner is in the window -- an old "
                    "build-loop.sh, or the round started further back than "
                    "window_mb reaches")
            # round=last is the DEFAULT, so a missing banner is not the caller
            # getting it wrong. Fall back to the whole window (what they would
            # have got before) and say so, rather than refusing to answer.
            if round_ == "last":
                return lines, 0, "whole window; " + note
            return lines, 0, "round=%s asked for, but %s" % (round_, note)
        if round_ == "last":
            index = len(rounds) - 1
        else:
            try:
                index = int(round_) - 1
                if not 0 <= index < len(rounds):
                    raise IndexError(round_)
            except (ValueError, IndexError):
                return lines, 0, ("round=%s not found; %d round(s) in the window"
                                  % (round_, len(rounds)))
        picked = rounds[index]
        # Stop at the NEXT round, not at the end of the log. Slicing only the
        # front leaves every later round in scope, so round=5 also answered for
        # round 6 -- the exact misattribution `round` exists to prevent. For
        # round=last the two are the same, which is why it went unnoticed.
        stop = rounds[index + 1][0] if index + 1 < len(rounds) else len(lines)
        return lines[picked[0]:stop], picked[0], "round: " + describe_round(picked)
    parsed = parse_since(since, lines)
    if parsed is None:
        return lines, 0, ""
    start = window_from(lines, parsed)
    label = "since %s %s" % parsed
    # A `since` that selects everything, or nothing, is almost always a mistake
    # rather than an answer -- and silently returning the unfiltered window is
    # the worst outcome, because the caller then attributes another round's
    # failures to the one they asked about. That has happened.
    #
    # The usual cause is a timezone: a check's "Started ..." on GitHub is UTC
    # while aliBuild stamps the log in the BUILDER's local clock, so a UTC value
    # lands an hour or two before the window even opens and trims nothing.
    if start == 0:
        label += (" -- WARNING: nothing was trimmed, every line is already at or "
                  "after it. The log is stamped in the BUILDER's local clock; a "
                  "UTC time (e.g. from a GitHub status) will be too early. "
                  "Prefer ci_rounds to get a round number and pass round=")
    elif start >= len(lines):
        label += (" -- WARNING: this is after the entire window, so nothing is "
                  "left. Pass an earlier time, or raise window_mb")
    return lines[start:], start, label


def window_note(lines, truncated, extra=""):
    """One line saying what was actually read, for every tool that reads a log.

    Without this the reader cannot tell "not in the log" from "not in the part
    of the log I looked at".
    """
    bits = []
    if extra:
        bits.append(extra)
    span = log_span(lines)
    if span:
        bits.append("covering %s" % span)
    if truncated:
        bits.append("WINDOW FULL -- older output not read; raise window_mb to "
                    "look further back")
    return "  [%s]" % "; ".join(bits) if bits else ""


def github_graphql(query, **variables):
    addr, token = _gate("github")
    reply = requests.post(addr + "/github/graphql", timeout=60,
                          headers={"Authorization": "Bearer " + token},
                          json={"query": query, "variables": variables})
    reply.raise_for_status()
    body = reply.json()
    if "errors" in body:
        raise RuntimeError(json.dumps(body["errors"])[:400])
    return body["data"]


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


def clip(line):
    line = line.rstrip("\n")
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + " ...[clipped]"


#: How many following lines carry the message, per kind. A compiler puts its
#: complaint on the line that matched, so pulling context there is pure noise.
#: CMake does the opposite: "CMake Error at FetchContent.cmake:2106 (message):"
#: says only WHERE, and every word of WHY is on the lines beneath it -- reported
#: alone it is unactionable, which is exactly how it first came back here.
BODY_LINES = {"cmake": 6, "test": 1, "fst-command": 2}


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


def strip_alibuild_prefix(line):
    """Drop aliBuild's "2026-08-19@21:17:45:DEBUG:O2:GEANT4:slc10_x86-64-...:".

    Two identical compiler errors from two packages are the same error; leaving
    the timestamp on makes every line unique and defeats deduplication.
    """
    return re.sub(r"^\d{4}-\d\d-\d\d@[\d:]+:[A-Z]+:[^:]*:[^:]*:[^:]*:\s*", "", line)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

#: The heartbeat claim-builder.sh prints on stdout every few minutes of a build.
HEARTBEAT_RE = re.compile(r"^(\S+) \[round (\d+)\] check=(\S+) pr=(\S+) "
                          r"sha=(\S+) elapsed=(\d+)m")


def current_claim(lines):
    """What a worker is on, from its stdout: (busy, description).

    The latest ROUND BEGIN with no END is the round in progress; if the latest
    has ENDed the worker is between rounds. Nothing else in the log can be
    trusted for this -- a GitHub status naming the allocation goes stale the
    moment the worker moves on, and the last PR number anywhere in the log
    belongs to whatever printed last, which on a claim worker is usually a lost
    claim for something else.
    """
    claims = find_claims(lines)
    claim = claims[-1] if claims else None
    if claim and claim["result"] is None:
        return True, "%s pr=%s@%.8s type=%s since %s" % (
            claim["check"], claim["pr"], claim["sha"], claim["type"],
            claim["when"][1])
    beat = None
    for raw in reversed(lines):
        beat = HEARTBEAT_RE.match(raw)
        if beat or " ===== ROUND " in raw:
            break
    if beat:
        # The BEGIN scrolled out of the tail, but a heartbeat after the last
        # banner can only belong to a round still running.
        return True, "%s pr=%s@%.8s  %sm in" % beat.group(3, 4, 5, 6)
    if not claim:
        return False, "no ROUND banner in the stdout tail"
    held = [c for c in claims if c["result"] != "claim-lost"]
    last = (" (last: %s pr=%s %s)" % (held[-1]["check"].rsplit("/", 1)[-1],
                                      held[-1]["pr"], claim_class(held[-1], False))
            if held else "")
    return False, "idle since %s%s" % (claim["when"][1], last)


@mcp.tool()
def ci_workers() -> str:
    """What every running CI checker allocation is doing right now.

    One line per worker: job, allocation, node, the claim it holds (from the
    latest ROUND BEGIN with no END on its stdout) or "idle" between rounds, and
    how far into the build it is -- counted only from output after that BEGIN,
    so a previous round's [n/m] is never shown for the current one.
    """
    out = []
    for job in nomad_get("/v1/jobs"):
        if not job["ID"].startswith("ci-"):
            continue
        for alloc in nomad_get("/v1/job/%s/allocations" % job["ID"]):
            if alloc["ClientStatus"] != "running":
                continue
            short = alloc["ID"][:8]
            line = ["%-16s %s on %s" % (job["ID"], short, alloc["NodeName"])]
            task = main_task(alloc)
            if task is None:
                out.append(line[0] + "  (no running task)")
                continue
            try:
                index, _ = nomad_log(alloc["ID"], task, stream="stdout",
                                     max_bytes=512 * 1024)
                log, _ = nomad_log(alloc["ID"], task, max_bytes=2 * 1024 * 1024)
            except Exception as err:            # noqa: BLE001
                # Never echo the URL: it carries the broker's ephemeral port and
                # is noise in a summary line.
                out.append(line[0] + "  (no log for task %s: %s)"
                           % (task, type(err).__name__))
                continue
            # Identity from the stdout index; progress from the stderr tail, but
            # only after this round's BEGIN trace: before it is another round.
            busy, what = current_claim(index.splitlines())
            trouble = task_trouble(alloc["TaskStates"][task])
            line.append(trouble or what)
            lines = log.splitlines() if busy and not trouble else []
            claims = find_claims(lines)
            log = "\n".join(lines[claims[-1]["start"]:] if claims else lines)
            package = re.findall(r"DEBUG:[^:]*:([^:]+):(?:slc|osx|ubuntu)", log)
            progress = re.findall(r"\[(\d+)/(\d+)\]", log)
            if package:
                line.append("building %s" % package[-1])
            if progress:
                line.append("[%s/%s]" % progress[-1])
            out.append("  ".join(line))
    return "\n".join(out) or "no running ci-* allocations"


@mcp.tool()
def ci_build_errors(alloc: str, task: str = "ci", max_errors: int = 12,
                    since: str = "", round: str = "last",
                    window_mb: int = 0) -> str:
    """Why this build failed: the distinct errors in its log, deduplicated.

    Applies ali-bot's own error patterns (the ones report-pr-errors uses to
    build the PR comment), strips aliBuild's per-line timestamp prefix so
    repeats collapse, and returns each distinct error once with a count. A
    build that fails 400 times for one reason reports one line, not 400.

    alloc may be a short id prefix, as with the nomad CLI.

    SCOPE IT ON A CLAIM-BASED WORKER. Those allocations are long-lived and their
    log holds many rounds for many PRs and several checks, so unscoped you get
    this round's failure next to an unrelated one from yesterday with nothing to
    tell them apart.

    PREFER `round`: call ci_rounds first and pass the number back. It is exact.

    `since` is the fallback and is easy to get wrong: the log is stamped in the
    BUILDER's local clock, while a check's "Started ..." on GitHub is UTC, so
    passing the latter unconverted selects the whole window and silently scopes
    nothing. The reply now says when that happened -- believe it.

    `window_mb` overrides how far back to read (default 64). The reply always
    says what was covered and whether the window filled up.
    """
    alloc_id = _resolve_alloc(alloc)
    log, truncated = nomad_log(alloc_id, task,
                               max_bytes=(window_mb or 0) * 1024 * 1024
                               or MAX_FETCH_BYTES)
    hits = {}                     # key -> [count, sample, kind, last_position]
    kinds, skipped = Counter(), 0
    lines = log.splitlines()
    lines, start, scope_label = scope_lines(lines, since, round)
    # A full window only hides something if it could hide what was ASKED for.
    # start > 0 means the window already reaches back past the requested round
    # or time, so whatever fell off the front is older than what was asked for
    # -- warning about it just sends the reader re-fetching for nothing.
    if (since or round) and start > 0:
        truncated = False
    for position, raw in enumerate(lines):
        line = strip_alibuild_prefix(raw)
        if NOISE_RE.search(line):
            skipped += 1
            continue
        for kind, pattern in PATTERNS.items():
            if pattern.search(line):
                # Numbers vary between otherwise identical errors (line
                # numbers, template depths, addresses); collapse them so the
                # same failure in twenty files groups into one entry.
                key = (kind, re.sub(r"\d+", "#", line)[:MAX_LINE_CHARS])
                if key in hits:
                    hits[key][0] += 1
                    hits[key][3] = position
                else:
                    hits[key] = [1, with_body(lines, position, kind), kind,
                                 position]
                kinds[kind] += 1
                break
    if not hits:
        return ("no recognised errors in %s (%d lines scanned, %d suppressed as "
                "noise).%s\nThe build may have been killed rather than failed -- "
                "try ci_log_tail."
                % (alloc_id[:8], len(lines), skipped,
                   window_note(lines, truncated, scope_label)))

    def severity(entry):
        """Rank fatal-and-late above frequent.

        Sorting by count put ten copies of a harmless message above the two
        copies of the one that stopped the build -- and the decisive error is
        usually the LAST one, since everything after it is teardown. So: the
        kinds that are always serious first, then anything that looks
        build-stopping, then latest-first within each tier. Count is reported
        but never ranks.
        """
        count, sample, kind, position = entry
        if kind in ("killed", "cmake", "test", "fst-timeout",
                    "fst-logfile", "fst-command"):
            tier = 0
        elif FATAL_RE.search(sample):
            tier = 1
        else:
            tier = 2
        return (tier, -position)

    ranked = sorted(hits.values(), key=severity)
    head = "%d distinct error(s) in %s, %s" % (
        len(hits), alloc_id[:8],
        ", ".join("%s=%d" % kv for kv in kinds.most_common()))
    if skipped:
        head += " (%d noise lines suppressed)" % skipped
    head += window_note(lines, truncated, scope_label)
    out = [head]
    for count, sample, kind, _ in ranked[:max_errors]:
        body = "\n".join(clip(l) for l in sample.splitlines())
        out.append("  [%s%s] %s"
                   % (kind, " x%d" % count if count > 1 else "", body))
    if len(ranked) > max_errors:
        out.append("  ... and %d more distinct errors" % (len(ranked) - max_errors))
    return "\n".join(out)


@mcp.tool()
def ci_log_search(alloc: str, pattern: str, task: str = "ci",
                  max_matches: int = 20, since: str = "", round: str = "last",
                  context: int = 0, oldest_first: bool = False,
                  window_mb: int = 0) -> str:
    """Search one allocation's log, returning counts and a capped sample.

    Deliberately not a grep: it reports how many lines matched and shows at
    most max_matches of them, clipped. Ask a broad question here without the
    answer arriving as several hundred kilobytes.

    SCOPED TO THE LAST ROUND BY DEFAULT. A claim-based worker's log holds many
    rounds for many PRs and several checks, and matches from a finished round
    read exactly like the running one: a stale `--threads 4` and a stale
    "[1249/1255]" were once reported as the current build's state when they
    belonged to the previous round, which had already failed. Pass round="all"
    to search the whole window, a number from ci_rounds for a specific one, or
    a `since` ("HH:MM" or "YYYY-MM-DD HH:MM", builder-local), which the default
    round="last" does not override.

    Shows the LAST matches, not the first; pass oldest_first=True to flip that.

    `context` adds that many lines either side of each match, like grep -C:
    the fatal line is often a compiler warning cascade above a terse make
    error, and the match alone does not say which package was building.
    """
    alloc_id = _resolve_alloc(alloc)
    regex = re.compile(pattern)
    log, truncated = nomad_log(alloc_id, task,
                               max_bytes=(window_mb or 0) * 1024 * 1024
                               or MAX_FETCH_BYTES)
    lines = log.splitlines()
    lines, start, scope_label = scope_lines(lines, since, round)
    # A full window only hides something if it could hide what was ASKED for.
    # start > 0 means the window already reaches back past the requested round
    # or time, so whatever fell off the front is older than what was asked for
    # -- warning about it just sends the reader re-fetching for nothing.
    if (since or round) and start > 0:
        truncated = False
    positions = [index for index, raw in enumerate(lines) if regex.search(raw)]
    if not positions:
        return ("no match for %r in %s%s"
                % (pattern, alloc_id[:8], window_note(lines, truncated, scope_label)))

    cap = min(max_matches, MAX_RETURN_LINES)
    shown = positions[:cap] if oldest_first else positions[-cap:]
    head = "%d line(s) match %r in %s" % (len(positions), pattern, alloc_id[:8])
    if len(positions) > len(shown):
        head += " (showing %s %d)" % ("first" if oldest_first else "last",
                                      len(shown))
    head += window_note(lines, truncated, scope_label)

    out, previous_end = [head], None
    for position in shown:
        start, end = max(0, position - context), min(len(lines),
                                                     position + context + 1)
        # Only worth separating blocks when there are blocks: at context=0 every
        # non-adjacent match would get a rule, which is noise.
        if context and previous_end is not None and start > previous_end:
            out.append("  --")
        for index in range(max(start, previous_end or 0), end):
            marker = "  " if index == position else "   "
            out.append(marker + clip(strip_alibuild_prefix(lines[index])))
        previous_end = end
    return "\n".join(out)


@mcp.tool()
def ci_rounds(alloc: str, task: str = "ci", window_mb: int = 0,
              show_lost: bool = False, max_rounds: int = 40) -> str:
    """What this worker has actually been doing, round by round.

    Reads the ROUND BEGIN/END banners claim-builder.sh prints around every claim
    attempt, so it sees the rounds that never reach a build as well: one line
    per round with its time, outcome, duration, build type (untested / failed /
    succeeded) and what it was for. Outcomes:

      ok            built, rc=0
      FAILED rc=N   built and failed
      verdict-only  rc=4: build-loop.sh posted "merge conflicts" or "too big"
      status-cap    the commit has hit GitHub's status limit; nothing built
      stale-skip / closed / not-head   build-one.sh re-read the status and
                    found the listing out of date; nothing built
      claim-lost    another worker held the claim (hidden unless show_lost)
      running       no END yet; no-END is an earlier round that never got one,
                    i.e. the worker was killed or restarted mid-round

    Where build-loop.sh printed its `ci-round:` block inside the round, the line
    carries it instead: the merged PR and the resolved commit of every checkout,
    including alidist -- which answers "was my recipe fix in this build?".

    Use this BEFORE ci_build_errors on a claim-based worker: the round number it
    prints can be passed straight back as `round`, which beats guessing a
    `since` timestamp from a GitHub status in another timezone. Numbers count
    the rounds in the window that held their claim, so they shift when one
    falls off the front: use them soon, with the same window_mb.
    """
    alloc_id = _resolve_alloc(alloc)
    log, truncated = nomad_log(alloc_id, task,
                               max_bytes=(window_mb or 0) * 1024 * 1024
                               or MAX_FETCH_BYTES)
    lines = log.splitlines()
    claims = find_claims(lines)
    if claims:
        attach_provenance(claims, lines)
        classes = [claim_class(claim, position + 1 == len(claims))
                   for position, claim in enumerate(claims)]
        tally = Counter(name.split()[0] for name in classes)
        numbers = {position: number for number, position
                   in enumerate(held_claims(claims), start=1)}
        rows = [("round=%d" % numbers[position] if position in numbers else "lost",
                 claim, name) for position, (claim, name)
                in enumerate(zip(claims, classes))
                if show_lost or position in numbers]
        out = ["%d round(s) in %s (%s)%s"
               % (len(numbers), alloc_id[:8],
                  ", ".join("%d %s" % (count, name)
                            for name, count in sorted(tally.items())),
                  window_note(lines, truncated))]
        if len(rows) > max_rounds:
            out.append("  ... %d earlier round(s) not shown" % (len(rows) - max_rounds))
        for number, claim, name in rows[-max_rounds:]:
            out.append("  [%s] %s %-12s %-7s %-10s %s"
                       % (number, claim["when"][1], name,
                          human_duration(claim["seconds"]), claim["type"],
                          describe_claim(claim)))
        return "\n".join(out)
    # No claim banners: a worker on an older loop. Fall back to ci-round:.
    rounds = find_rounds(lines)
    if not rounds:
        return ("no ROUND or ci-round: banner in %s%s\nEither the worker predates the "
                "banner in build-loop.sh, or every round in the window started "
                "before it -- try a larger window_mb."
                % (alloc_id[:8], window_note(lines, truncated)))
    stamps = stamp_index(lines)
    verdicts = []
    for position, entry in enumerate(rounds):
        stop = rounds[position + 1][0] if position + 1 < len(rounds) else len(lines)
        verdict, seconds = round_outcome(lines, entry[0], stop, stamps)
        if verdict is None:
            # The last round has simply not finished; an earlier one that never
            # set PR_OK did not finish either, which is a different thing worth
            # seeing -- it means the worker died or was restarted mid-build.
            verdict = "running" if position + 1 == len(rounds) else "no verdict"
        verdicts.append((verdict, seconds))

    tally = {}
    for verdict, _ in verdicts:
        tally[verdict] = tally.get(verdict, 0) + 1
    summary = ", ".join("%d %s" % (count, name)
                        for name, count in sorted(tally.items()))
    out = ["%d round(s) in %s (%s)%s"
           % (len(rounds), alloc_id[:8], summary,
              window_note(lines, truncated))]
    for number, (entry, (verdict, seconds)) in enumerate(zip(rounds, verdicts),
                                                         start=1):
        stamp = stamp_from(stamps, entry[0], forward=True)
        out.append("  [round=%d] %s %-10s %-7s %s"
                   % (number, stamp[1] if stamp else "        ", verdict,
                      human_duration(seconds), describe_round(entry)))
    return "\n".join(out)


#: The jobs that run claim-builder.sh.
CLAIM_JOBS = ("ci-linux-x86", "ci-linux-x86-v2", "ci-linux-arm",
              "ci-linux-x86-gpu", "ci-linux-x86-release", "ci-macos-arm")

#: The entrypoint pip-installs ali-bot into a fresh allocation venv before the
#: loop starts, and pip says which commit it resolved and what it installed.
#: build-one.sh's per-check installs print the same, hence "before the first
#: BEGIN after the start".
ALIBOT_RESOLVED_RE = re.compile(
    r"Resolved https://github\.com/(\S+/ali-bot)\S* to commit ([0-9a-f]{7,})")
ALIBOT_INSTALLED_RE = re.compile(r"^Successfully installed .*\bali-bot-(\S+)")
#: What says a worker is unhealthy, matched only on lines that are not `set -x`
#: traces (a trace of the printf is not the event).
HEALTH_RE = {
    "cooldown": re.compile(r"^\S+ ===== COOLDOWN "),
    "traceback": re.compile(r"^Traceback \(most recent call last\)"),
    "ModuleNotFound": re.compile(r"^ModuleNotFoundError"),
    "cleanup-impossible": re.compile(r"^\S+ ===== CLEANUP IMPOSSIBLE"),
}
FREE_RE = re.compile(r"^cleanup\.py: \w+: .*?([\d.]+ GiB) free"
                     r"|^\S+ ===== CLEANUP IMPOSSIBLE: .*, (\S+) free =====")
#: Only for a task never restarted, whose LastRestart carries no UTC offset.
BUILDER_TZ = "Europe/Zurich"


def task_start(state):
    """(date, time) of a task's latest (re)start in the BUILDER's clock, or None.

    Nomad gives StartedAt in UTC; the log is stamped in local time. LastRestart
    is serialised in the Nomad client's own zone, which is the host's, and the
    ci task is raw_exec, so that offset is the log's.
    """
    from zoneinfo import ZoneInfo

    def parse(value):
        # Nomad has nanoseconds; fromisoformat stops at microseconds.
        value = re.sub(r"(\.\d{6})\d+", r"\1", value or "").replace("Z", "+00:00")
        return datetime.fromisoformat(value)
    try:
        started = parse(state.get("StartedAt"))
        zone = (parse(state["LastRestart"]).tzinfo if state.get("LastRestart")
                else ZoneInfo(BUILDER_TZ))
    except ValueError:
        return None
    if started.year < 2000:               # 0001-01-01: never started
        return None
    local = started.astimezone(zone)
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S")


def task_trouble(state):
    """"" for a running task, else its state and Nomad's latest word on it.

    An allocation can be "running" while its main task crash-loops: on
    2026-11-13 the GPU worker's had restarted 6108 times, and its stdout still
    ended in a BEGIN from before, which read as a 17-hour build.
    """
    if state.get("State") == "running":
        return ""
    events = state.get("Events") or [{}]
    return "TASK %s: %.80s" % (state.get("State"),
                               events[-1].get("DisplayMessage") or "")


def since_start(lines, start):
    """Index of the first line after the last one stamped before `start`."""
    cut = 0
    for index, raw in enumerate(lines if start else ()):
        stamp = line_time(raw)
        if stamp and stamp < start:
            cut = index + 1
    return cut


def rollout_line(job_id, alloc, window_mb):
    """ci_rollout's line for one allocation."""
    head = "%-20s %s %-18s" % (job_id, alloc["ID"][:8], alloc["NodeName"])
    task = main_task(alloc)
    state = (alloc.get("TaskStates") or {}).get(task)
    if alloc["ClientStatus"] != "running" or not state:
        return head + "  " + alloc["ClientStatus"]
    start = task_start(state)
    bits = ["started %s %s" % (start[0][5:], start[1][:5]) if start
            else "start unknown", "restarts=%d" % state.get("Restarts", 0)]
    if task_trouble(state):
        bits.append(task_trouble(state))
    try:
        index, index_full = nomad_log(alloc["ID"], task, stream="stdout",
                                      max_bytes=4 * 1024 * 1024)
        log, log_full = nomad_log(alloc["ID"], task,
                                  max_bytes=window_mb * 1024 * 1024)
    except Exception as err:                # noqa: BLE001
        return head + "  (no log for task %s: %s)" % (task, type(err).__name__)

    # stdout is the round index and reaches back far; stderr is the firehose.
    out_lines = index.splitlines()
    out_cut = since_start(out_lines, start)
    out_lines = out_lines[out_cut:]
    err_lines = log.splitlines()
    err_cut = since_start(err_lines, start)
    err_lines = err_lines[err_cut:]
    err_claims = find_claims(err_lines)
    claims = find_claims(out_lines) or err_claims

    version = "unknown"
    if out_cut or not index_full:           # the window reaches the start
        pre = out_lines[:claims[0]["start"]] if claims and claims is not err_claims \
            else out_lines
        resolved = installed = None
        for raw in pre:
            resolved = ALIBOT_RESOLVED_RE.search(raw) or resolved
            installed = ALIBOT_INSTALLED_RE.match(raw) or installed
        if resolved:
            version = "%s@%.8s" % resolved.groups()
        elif installed:
            version = "ali-bot-" + installed.group(1)
    else:
        version = "unknown (start not in window)"
    bits.append(version)

    # The status-cap skip is only visible on stderr; carry it over by banner.
    capped = {(c["when"], c["check"], c["pr"]) for c in err_claims if c["status_cap"]}
    for claim in claims:
        claim["status_cap"] |= (claim["when"], claim["check"], claim["pr"]) in capped
    tally = Counter(claim_class(claim, position + 1 == len(claims)).split()[0]
                    for position, claim in enumerate(claims))
    bits.append("rounds: " + (", ".join("%d %s" % (count, name)
                                        for name, count in sorted(tally.items()))
                              or "none"))

    health, free = Counter(), None
    for raw in err_lines:
        if raw.startswith("+"):
            continue
        for name, pattern in HEALTH_RE.items():
            if pattern.match(raw):
                health[name] += 1
        found = FREE_RE.match(raw)
        if found:
            free = found.group(1) or found.group(2)
    bits.append(" ".join("%s=%d" % (name, health[name]) for name in HEALTH_RE))
    bits.append("free=%s" % (free or "?"))
    # Counted from the window, not from the start, when it does not reach back.
    notes = ["%s from %s" % (what, log_span(lines).split(" to ")[0] or "?")
             for what, lines, cut, full in (("rounds", out_lines, out_cut, index_full),
                                            ("stderr counts", err_lines, err_cut, log_full))
             if full and not cut]
    if notes:
        bits.append("[only %s]" % "; ".join(notes))
    return head + "  " + "  ".join(bits)


@mcp.tool()
def ci_rollout(job: str = "", window_mb: int = 8) -> str:
    """Did the restart pick up the new code, and is it healthy? One call.

    One line per allocation of `job` (default: every claim-based ci-* job):
    node, when its task last (re)started and how often (from Nomad, shown in
    the builder's clock), the ali-bot commit the entrypoint installed (from
    pip's "Resolved ... to commit" before the first round; "unknown" if that
    is not in the window), and SINCE THAT START: rounds by outcome as in
    ci_rounds, COOLDOWN banners, Python Tracebacks, ModuleNotFoundError,
    CLEANUP IMPOSSIBLE, and the last free-space figure cleanup.py printed.

    Rounds come from stdout, which is small and usually reaches back to the
    start; the other counts need stderr, of which only the last `window_mb` MB
    are read per allocation. When that does not reach the start the line says
    from when it counted.
    """
    from concurrent.futures import ThreadPoolExecutor
    work = []
    for job_id in [job] if job else CLAIM_JOBS:
        try:
            allocs = nomad_get("/v1/job/%s/allocations" % job_id)
        except requests.HTTPError:
            work.append((job_id, None))
            continue
        work += [(job_id, alloc) for alloc in allocs
                 if alloc["ClientStatus"] in ("running", "pending")]

    def one(item):
        job_id, alloc = item
        if alloc is None:
            return "%-20s (no such job)" % job_id
        try:
            return rollout_line(job_id, alloc, window_mb)
        except Exception as err:            # noqa: BLE001
            return "%-20s %s  (failed: %s)" % (job_id, alloc["ID"][:8],
                                              type(err).__name__)
    with ThreadPoolExecutor(max_workers=8) as pool:
        lines = list(pool.map(one, work))
    return "\n".join(lines) or "no running allocations in %s" % (job or "the ci-* jobs")


@mcp.tool()
def ci_log_tail(alloc: str, task: str = "ci", lines: int = 30) -> str:
    """The last few lines of a log, clipped. For "what is it doing right now"."""
    alloc_id = _resolve_alloc(alloc)
    lines = min(lines, MAX_RETURN_LINES)
    log, _ = nomad_log(alloc_id, task, max_bytes=1024 * 1024)
    tail = log.splitlines()[-lines:]
    return "\n".join(clip(strip_alibuild_prefix(line)) for line in tail) \
        or "(empty log)"


@mcp.tool()
def ci_check_status(check_name: str, repo: str) -> str:
    """How one check is doing across every open PR of a repo.

    Returns the per-PR verdicts plus the two aggregates that individual status
    pages cannot show: how the states break down, and whether many PRs are
    failing for the SAME stated reason -- which is the signature of a broken
    worker or image rather than of broken pull requests.
    """
    owner, _, name = repo.partition("/")
    data = github_graphql("""
    query($o:String!,$n:String!){repository(owner:$o,name:$n){
      pullRequests(states:OPEN,first:100,orderBy:{field:UPDATED_AT,direction:DESC}){
        nodes{number isDraft
          commits(last:1){nodes{commit{status{contexts{
            context state description createdAt}}}}}}}}}
    """, o=owner, n=name)
    rows, reasons = [], Counter()
    for pull in data["repository"]["pullRequests"]["nodes"]:
        commits = pull["commits"]["nodes"]
        status = commits[0]["commit"]["status"] if commits else None
        if not status:
            continue
        for ctx in status["contexts"]:
            if ctx["context"] != check_name:
                continue
            rows.append((ctx["createdAt"], pull["number"], ctx["state"],
                         (ctx["description"] or "")[:60]))
            if ctx["state"] in ("ERROR", "FAILURE") and (ctx["description"] or "").strip():
                reasons[ctx["description"].strip()] += 1
    if not rows:
        return "%s has posted on no open PR of %s" % (check_name, repo)
    rows.sort()
    out = ["%s on %s: %s" % (check_name, repo,
                             dict(Counter(r[2] for r in rows)))]
    for when, number, state, description in rows[-MAX_RETURN_LINES:]:
        out.append("  PR %-7d %-9s %s  %s" % (number, state.lower(), when[:16],
                                              description))
    for reason, count in reasons.most_common(3):
        if count >= 3:
            out.append("  NOTE: %d PRs failed with the same reason %r -- that is "
                       "usually the environment, not the PRs" % (count, reason))
    return "\n".join(out)


#: Where report-pr-errors uploads a build's log. This is the ONLY durable record
#: of a build: Nomad garbage-collects allocations out of `job status
#: -all-allocs` within hours, and a busy claim worker's log rotates inside about
#: 45 minutes, but this survives. On 2026-09-04 it was the only thing that named
#: which machine had reddened ~70 pull requests -- Nomad had already forgotten
#: the allocation, and reading -all-allocs led to the WRONG machine.
BUILD_LOG_BUCKET = "alice-build-logs"


def s3_get(key, timeout=120):
    """One object from the CERN S3 through the security-proxy s3 route.

    The proxy signs for real; the client sends an effectively-unsigned SigV4
    whose access-key-id is the rotating gate token, which is what the proxy
    validates. Hence the placeholder signature -- it is never checked, and no
    real credential is present on this side.
    """
    addr, token = _gate("s3")
    reply = requests.get(
        "%s/%s/%s" % (addr, BUILD_LOG_BUCKET, key), timeout=timeout,
        headers={
            "Authorization": ("AWS4-HMAC-SHA256 Credential=%s/00000000/default/"
                              "s3/aws4_request, SignedHeaders=host, "
                              "Signature=unsigned" % token),
            "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
        })
    if reply.status_code == 404:
        return None
    reply.raise_for_status()
    return reply.text


@mcp.tool()
def ci_pr(repo: str, number: int) -> str:
    """Every check on a PR's head commit, and which of them are red."""
    owner, _, name = repo.partition("/")
    data = github_graphql("""
    query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){
      pullRequest(number:$p){ title mergeable headRefOid
        commits(last:1){nodes{commit{status{contexts{
          context state description targetUrl}}}}}}}}
    """, o=owner, n=name, p=number)
    pull = data["repository"]["pullRequest"]
    commits = pull["commits"]["nodes"]
    status = commits[0]["commit"]["status"] if commits else None
    contexts = status["contexts"] if status else []
    red = [c for c in contexts if c["state"] in ("ERROR", "FAILURE")]
    out = ["%s#%d %.60s" % (repo, number, pull["title"]),
           "  head %.8s  mergeable=%s  %d checks, %d red"
           % (pull["headRefOid"], pull["mergeable"], len(contexts), len(red))]
    for ctx in sorted(contexts, key=lambda c: (c["state"] != "ERROR", c["context"])):
        out.append("  %-9s %-44s %s" % (ctx["state"].lower(), ctx["context"],
                                        (ctx["description"] or "")[:40]))
    return "\n".join(out[:MAX_RETURN_LINES])


@mcp.tool()
def ci_build_log(repo: str, pr: int, check_name: str, sha: str = "",
                 max_errors: int = 12) -> str:
    """Why a check went red, starting from the PR rather than from a worker.

    Reads the log report-pr-errors uploaded to s3://alice-build-logs, which is
    the only durable record of a build. Use this whenever you have a red check
    and no allocation, or when the allocation is gone -- Nomad drops them from
    `job status -all-allocs` within hours and a claim worker's own log rotates
    in under an hour, while this does not.

    It answers WHICH MACHINE ran the build, which the GitHub status does not say
    and a garbage-collected allocation can no longer tell you.

    `sha` defaults to the PR's current head. Pass it explicitly to read an older
    attempt, since the path is keyed by commit.

    check_name is the GitHub context, e.g. "build/O2Physics/o2/macOS-arm".
    """
    owner, _, name = repo.partition("/")
    if not sha:
        data = github_graphql("""
        query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){
          pullRequest(number:$p){headRefOid}}}
        """, o=owner, n=name, p=pr)
        sha = data["repository"]["pullRequest"]["headRefOid"]
    key = "%s/%d/%s/%s/fullLog.txt" % (repo, pr, sha, check_name.replace("/", "_"))
    log = s3_get(key)
    if log is None:
        return ("no uploaded log at s3://%s/%s\n"
                "Either the check never ran for this commit, the name is wrong "
                "(it is the GitHub context, e.g. build/O2Physics/o2/macOS-arm), "
                "or the build was killed before report-pr-errors ran -- which "
                "leaves the PR on a stale 'pending' status."
                % (BUILD_LOG_BUCKET, key))

    lines = log.splitlines()
    out = ["%s#%d %s  (%.8s, %d bytes)" % (repo, pr, check_name, sha, len(log))]
    # The header report-pr-errors writes: host and allocation. This is the whole
    # reason to prefer this over the Nomad log.
    for raw in lines[:12]:
        if raw.startswith(("Finished building on", "Built commit",
                           "Nomad allocation:")) or raw.strip().startswith("http"):
            out.append("  " + clip(raw.strip()))

    # A few hundred bytes ending in "No logs found" is not a failed PR, it is a
    # broken machine: the build produced nothing at all. That was the signature
    # of the missing-SDK failure, and it reads as an ordinary red check.
    if len(log) < 2000 and "No logs found" in log:
        out.append("")
        out.append("  *** THE BUILD PRODUCED NO OUTPUT (%d bytes, 'No logs "
                   "found'). This is a BROKEN BUILDER, not a broken PR -- the "
                   "machine failed before compiling anything. Check the host "
                   "named above." % len(log))
        return "\n".join(out)

    hits, kinds, skipped = {}, Counter(), 0
    for position, raw in enumerate(lines):
        line = strip_alibuild_prefix(raw)
        if NOISE_RE.search(line):
            skipped += 1
            continue
        for kind, pattern in PATTERNS.items():
            if pattern.search(line):
                key_ = (kind, re.sub(r"\d+", "#", line)[:MAX_LINE_CHARS])
                if key_ in hits:
                    hits[key_][0] += 1
                    hits[key_][3] = position
                else:
                    hits[key_] = [1, with_body(lines, position, kind), kind,
                                  position]
                kinds[kind] += 1
                break
    if not hits:
        out.append("")
        out.append("  no recognised errors in %d lines (%d suppressed as noise)."
                   % (len(lines), skipped))
        return "\n".join(out)

    out.append("")
    out.append("  %d distinct error(s), %s" % (
        len(hits), ", ".join("%s=%d" % kv for kv in kinds.most_common())))
    # Latest-first: the decisive error is the last one, everything after it is
    # teardown. Same reasoning as ci_build_errors.
    for count, sample, kind, _ in sorted(hits.values(), key=lambda e: -e[3])[:max_errors]:
        body = "\n".join(clip(l) for l in sample.splitlines())
        out.append("  [%s%s] %s" % (kind, " x%d" % count if count > 1 else "", body))
    return "\n".join(out[:MAX_RETURN_LINES])


@mcp.tool()
def ci_image(image: str) -> str:
    """A builder image's entrypoint and PATH, read from the registry.

    No pull: it fetches the config blob directly, so checking a multi-gigabyte
    builder costs two small requests. It also flags an empty PATH component,
    which is what "${PATH}:/usr/local/cuda/bin" leaves behind when the base
    image declared no PATH -- the slc10-gpu-builder bug, which presented as
    every aliBuild doctor check failing.
    """
    repo, _, tag = image.partition(":")
    repo = repo.replace("registry.cern.ch/", "")
    tag = tag or "latest"
    base = "https://registry.cern.ch"
    token = requests.get("%s/service/token" % base, timeout=30, params={
        "service": "harbor-registry", "scope": "repository:%s:pull" % repo,
    }).json().get("token", "")
    headers = {"Authorization": "Bearer " + token,
               "Accept": "application/vnd.docker.distribution.manifest.v2+json,"
                         "application/vnd.oci.image.manifest.v1+json"}
    manifest = requests.get("%s/v2/%s/manifests/%s" % (base, repo, tag),
                            headers=headers, timeout=30).json()
    digest = manifest.get("config", {}).get("digest")
    if not digest:
        return "no image config for %s:%s (%s)" % (repo, tag, str(manifest)[:120])
    config = requests.get("%s/v2/%s/blobs/%s" % (base, repo, digest),
                          headers=headers, timeout=60).json()
    env = config.get("config", {}) or {}
    path = next((e for e in env.get("Env") or [] if e.startswith("PATH=")), None)
    out = ["%s:%s" % (repo, tag),
           "  created:    %s" % config.get("created"),
           "  entrypoint: %s   cmd: %s" % (env.get("Entrypoint"), env.get("Cmd")),
           "  %s" % (path or "PATH ABSENT")]
    if path and (path == "PATH=" or path[5:].startswith(":") or "::" in path):
        out.append("  ^^ EMPTY PATH COMPONENT: ${PATH} expanded to nothing from "
                   "the base image. Everything relying on PATH will fail.")
    return "\n".join(out)


def main_task(alloc):
    """Which task in this allocation carries the build.

    Task names differ per job -- "ci" on the Linux checkers, something else on
    the macOS ones -- and asking for the wrong one is a 500 from the log API
    rather than a useful error. Prefer a task literally called "ci", else the
    last one still running, which for a group with prestart sidecars is the
    main one.
    """
    states = alloc.get("TaskStates") or {}
    if "ci" in states:
        return "ci"
    running = [name for name, state in states.items()
               if state.get("State") == "running"]
    return running[-1] if running else (max(states) if states else None)


def _resolve_alloc(prefix):
    """Accept a short allocation id, as the nomad CLI does."""
    if len(prefix) >= 36:
        return prefix
    for job in nomad_get("/v1/jobs"):
        for alloc in nomad_get("/v1/job/%s/allocations" % job["ID"]):
            if alloc["ID"].startswith(prefix):
                return alloc["ID"]
    raise ValueError("no allocation starting with %r" % prefix)


@mcp.tool()
def ci_annotate(text: str, tags: str = "", dashboard_uid: str = "",
                panel_id: int = 0, time_ms: int = 0, time_end_ms: int = 0) -> str:
    """Mark a moment on the Grafana dashboards, so a change is visible next to
    its effect.

    A config change whose consequence shows up in a graph hours later is hard to
    attribute after the fact: the graph moves and nobody remembers what happened
    at that timestamp. An annotation puts a labelled line at the moment, on every
    panel whose dashboard queries the matching tag.

    This WRITES to the shared Grafana. Use it for things a colleague reading the
    dashboard next week would want explained -- a store repointed, a job resized,
    JOBS changed -- not for routine progress.

    `tags` is comma-separated. Prefer a stable vocabulary so dashboards can query
    it: "ci,slc10,store" rather than free text. Without a dashboard_uid this is an
    ORGANISATION annotation, which appears only on dashboards configured with an
    annotation query matching the tags; pass dashboard_uid (and optionally
    panel_id) to pin it to one dashboard instead.

    Times are epoch MILLISECONDS, not seconds -- Grafana silently treats a
    seconds value as 1970 and the annotation lands somewhere invisible. Both
    default to now; pass time_end_ms for a range (a build window, a rollout).
    """
    import time as _time
    now = int(_time.time() * 1000)
    start = time_ms or now
    body = {"time": start, "text": text}
    if time_end_ms:
        if time_end_ms < start:
            return ("time_end_ms (%d) is before time_ms (%d); refusing to write "
                    "a backwards range" % (time_end_ms, start))
        body["timeEnd"] = time_end_ms
    if tags:
        body["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if dashboard_uid:
        body["dashboardUID"] = dashboard_uid
    if panel_id:
        body["panelId"] = panel_id
    for field in ("time", "timeEnd"):
        # A seconds-vs-milliseconds mix-up is the classic way to lose an
        # annotation, and Grafana accepts it silently. Anything before 2001 in
        # ms is almost certainly a seconds value.
        if field in body and body[field] < 1_000_000_000_000:
            return ("%s=%d looks like epoch SECONDS; this API wants "
                    "milliseconds" % (field, body[field]))

    try:
        addr, token = _gate("grafana-annotate")
    except subprocess.CalledProcessError:
        return ("no 'grafana-annotate' route on the security-proxy. It needs a "
                "route to https://monit-grafana.cern.ch injecting a token with "
                "annotation-write scope, plus the matching ingest slot -- both "
                "are the human's to provision (see the security-proxy skill). "
                "Ask for a bogus service to list what is configured.")
    reply = requests.post(addr + "/grafana/api/annotations", json=body, timeout=60,
                          headers={"Authorization": token})
    if reply.status_code >= 400:
        return ("Grafana refused the annotation (HTTP %d): %s\nA 401/403 here is "
                "usually the token lacking annotation-write scope rather than the "
                "route being wrong." % (reply.status_code, clip(reply.text)))
    ident = ""
    try:
        ident = " id=%s" % reply.json().get("id", "")
    except ValueError:
        pass
    return "annotated at %d%s: %s" % (start, ident, text)



if __name__ == "__main__":
    mcp.run()
