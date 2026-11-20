"""sanitize.py: what a published ci-failures.db may contain, and what not.

    python test/sanitize_test.py
"""
import collections
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sanitize  # noqa: E402

failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


JWT = "eyJhbGciOiJSUzI1NiJ9" + ".eyJzdWIiOiJ0ZXN0LXVzZXIifQ" + ".c2lnbmF0dXJlLXZhbHVlLWhlcmU"
A1, A2 = "5cab146f-1111-2222-3333-444455556666", "314d9697-1111-2222-3333-444455556666"
HOSTS = {"alimetal07.cern.ch", "alibuildmac04.cern.ch", "aligpuci.cern.ch"}

# ---- the text rules (they still apply: fingerprints are made from rewritten text)
s, st = sanitize.Sanitizer(HOSTS), collections.Counter()
t = s.text("ccdb path Users/m/meide/Tests/alimetal07.cern.ch/pid-1 failed; peer alibuildmac04 also; "
           "ROOT 6.36.10.4 at 188.184.86.225 and 127.0.0.1; ali-qcdb-test.cern.ch:8080 down; "
           "alice-ccdb.cern.ch fine; see https://alinomad.cern.ch/ui/allocations/" + A1,
           "alimetal07.cern.ch", "alloc-5cab146f", st)
print("   ", t)
check("Tests/alloc-5cab146f/pid-1" in t, "a build's own machine becomes its allocation")
check("peer <host> also" in t, "another builder (short name) becomes <host>")
check("<host>:8080 down" in t, "another *.cern.ch name becomes <host>")
check("alice-ccdb.cern.ch fine" in t and "https://alinomad.cern.ch/ui/allocations/" + A1 in t,
      "public services and the allocation link are kept")
check("at <ip> and 127.0.0.1" in t and "ROOT 6.36.10.4" in t, "a CERN IP becomes <ip>; loopback and versions stay")
t = s.text("token " + JWT + " on alibuildmac04.cern.ch", "alibuildmac04.cern.ch", "alloc-314d9697", st)
check(JWT not in t and "<redacted>" in t and "alloc-314d9697" in t, f"secrets are scrubbed again ({t})")
check(st.get("secret:jwt") == 1, "and reported by kind")
t = s.text("no space left on aligpuci.cern.ch", "aligpuci.cern.ch", None, collections.Counter())
check(t == "no space left on <host>", f"without an allocation, its own machine is <host> ({t})")

# ---- a database as the flow writes it
work = Path(tempfile.mkdtemp(prefix="sanitize-"))
db = work / "ci-failures.db"
c = sqlite3.connect(db)
c.executescript("""
CREATE TABLE ci_builds (build_id TEXT NOT NULL PRIMARY KEY, host TEXT, alloc TEXT, log_url TEXT);
CREATE TABLE ci_errors (build_id TEXT NOT NULL, sig_hash TEXT NOT NULL, host TEXT, kind TEXT, sig_exact TEXT,
                        sig_class TEXT, file TEXT, line INTEGER, text TEXT, PRIMARY KEY (build_id, sig_hash));
CREATE TABLE ci_signatures (sig_hash TEXT NOT NULL PRIMARY KEY, kind TEXT, sig_exact TEXT, sig_class TEXT,
                            file TEXT, embedding BLOB);
CREATE TABLE scratch (k TEXT, v TEXT);
CREATE VIEW recent AS SELECT * FROM ci_errors;
CREATE INDEX errors_line ON ci_errors(line);
""")
c.executemany("INSERT INTO ci_builds VALUES (?, ?, ?, ?)", [
    ("b1", "alimetal07.cern.ch", A1, "https://ali-ci.cern.ch/alice-build-logs/x"),
    ("b2", "alibuildmac04.cern.ch", A2, None),
    ("b3", "aligpuci.cern.ch", None, None),          # a log without its allocation
])
LOGTEXT = "raw log text: the body of an error, never published"
CCDB = "[FATAL] Unable to find CCDB object Users/m/meide/Calib on alimetal07.cern.ch"
DIAG = "error: unused variable '_' [-Wunused-variable]"
CRASH = "crash in its-tracker_N (o2-its-reco-workflow): o2::its::Tracker::clustersToTracks"
c.executemany("INSERT INTO ci_errors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [
    ("b1", "s1", "alimetal07.cern.ch", "error", CCDB, CCDB, None, 12, LOGTEXT),
    ("b1", "s2", "alimetal07.cern.ch", "error", "PWGLF/Tasks/x.cxx: " + DIAG.replace("'_'", "'n'"), DIAG,
     "PWGLF/Tasks/x.cxx", 40, LOGTEXT),
    ("b2", "s3", "alibuildmac04.cern.ch", "crash", CRASH, "crash: o2::its::Tracker::clustersToTracks", None, 7, LOGTEXT),
    ("b2", "s4", "alibuildmac04.cern.ch", "error", "/home/meide/x.cxx: " + DIAG, DIAG, "/home/meide/x.cxx", 9, LOGTEXT),
    ("b3", "s5", "aligpuci.cern.ch", "error", "x.cxx: error: in '_': check a == b has failed",
     "error: in '_': check a == N has failed", "test/x.cxx", None, LOGTEXT),
])
c.execute("INSERT INTO ci_signatures VALUES ('s1', 'error', ?, ?, NULL, x'00ff')", (CCDB, CCDB))
c.execute("INSERT INTO scratch VALUES ('k', ?)", (LOGTEXT,))
c.commit()
c.close()

stats = sanitize.sanitize_db(db, templates=None)   # srctemplates has its own test
print("stats:", stats)
c = sqlite3.connect(db)
hosts = dict(c.execute("SELECT build_id, host FROM ci_builds"))
check(hosts == {"b1": "alloc-5cab146f", "b2": "alloc-314d9697", "b3": None},
      f"ci_builds.host is the allocation, NULL without one ({hosts})")
check({h for (h,) in c.execute("SELECT DISTINCT host FROM ci_errors")} == set(hosts.values()),
      "ci_errors.host follows its build")
fp = dict(c.execute("SELECT sig_hash, fingerprint FROM ci_errors"))
check(fp == {"s1": None, "s2": DIAG, "s3": CRASH, "s4": None, "s5": None},
      "readable fingerprints only for a crash and a compiler diagnostic on a public file; "
      f"runtime messages, Boost.Test and a file in a home directory are hash-only ({fp})")
files = dict(c.execute("SELECT sig_hash, file FROM ci_errors"))
check(files["s2"] == "PWGLF/Tasks/x.cxx" and files["s4"] is None and files["s5"] == "test/x.cxx",
      f"file paths are published unless they could name a person ({files})")
check(dict(c.execute("SELECT sig_hash, line FROM ci_errors")) == {"s1": 12, "s2": 40, "s3": 7, "s4": 9, "s5": None},
      "the pointers into the logs are kept")
check(c.execute("SELECT fingerprint, embedding FROM ci_signatures").fetchone() == (None, b"\x00\xff"),
      "signatures: no text, the embedding kept (searched on the server, never returned)")

# Deny by default: only what sanitize.PUBLISHED lists is left.
objs = sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
check(objs == ["ci_builds", "ci_errors", "ci_signatures"], f"unlisted tables, views and indexes are dropped ({objs})")
cols = {t: [r[1] for r in c.execute(f"PRAGMA table_info({t})")] for t in objs}
FP = ["fingerprint", "fingerprint_kind", "fingerprint_src", "template_id", "filler_hash", "fillers_public"]
check(cols == {"ci_builds": ["build_id", "host", "log_url"],
               "ci_errors": ["build_id", "sig_hash", "host", "kind", "file", "line", *FP],
               "ci_signatures": ["sig_hash", "kind", "file", "embedding", *FP]},
      f"no log text, no signature text, no full allocation id ({cols})")
check(c.execute("SELECT count(*) FROM ci_errors WHERE fillers_public IS NOT NULL").fetchone()[0] == 0,
      "PUBLIC_FILLERS off: the public values are not published")
c.close()
check(stats.get("dropped column ci_errors.text") == 1 and stats.get("dropped column ci_errors.sig_exact") == 1
      and stats.get("dropped table scratch") == 1 and stats.get("ci_errors: file withheld") == 1,
      "and the drops are reported")
raw = db.read_bytes()
left = [n for n in (b"alimetal07", b"alibuildmac04", b"aligpuci", b"meide", b"CCDB object", b"check a",
                    LOGTEXT.encode(), A1.encode()) if n in raw]
check(not left, f"no trace in the file's bytes, free pages included ({left})")

again = work / "again.db"
shutil.copyfile(db, again)
check(sanitize.sanitize_db(again, templates=None) == {}, "a published DB is left as it is")

# A column the list does not name, which cannot be dropped, stops the publish.
odd = work / "odd.db"
c = sqlite3.connect(odd)
c.executescript("CREATE TABLE ci_builds (build_id TEXT, host TEXT, alloc TEXT);"
                "CREATE TABLE ci_errors (build_id TEXT, sig_hash TEXT, host TEXT, secret_body TEXT,"
                " PRIMARY KEY (build_id, secret_body));")
c.close()
try:
    sanitize.sanitize_db(odd, templates=None)
    check(False, "an undroppable unlisted column must fail the publish")
except Exception as e:
    check("secret_body" in str(e) or isinstance(e, sqlite3.OperationalError),
          f"an undroppable unlisted column fails the publish ({type(e).__name__}: {e})")

# A compiler diagnostic's names (the '_' of its template) are its fillers: hashed
# with the key, the same name in two files grouped, never shown.
import srctemplates  # noqa: E402
srctemplates.build(str(work), [])
T = srctemplates.load(str(work), "k" * 32)
ex = {n: f"{f}:3:5: error: unused variable '{v}' [-Wunused-variable]"
      for n, f, v in [("a", "x.cxx", "nTracks"), ("b", "y.cxx", "nTracks"), ("c", "x.cxx", "nClusters")]}
pp = {n: sanitize.public_part("error", e, DIAG, "x.cxx", T) for n, e in ex.items()}
check(pp["a"]["filler_hash"] == pp["b"]["filler_hash"] != pp["c"]["filler_hash"]
      and len({p["template_id"] for p in pp.values()}) == 1,
      f"compiler: one template, the filler hash tells the names apart ({pp})")
check(sanitize.compiler_fillers(ex["a"]) == ["nTracks"] and "nTracks" not in str(pp),
      "the name is the filler, and only its keyed hash is published")
check(sanitize.public_part("error", ex["a"], DIAG, "x.cxx", srctemplates.load(str(work)))["filler_hash"] is None,
      "without a key, no filler hash")
check(sanitize.public_part("error", CRASH, CRASH, None, T)["filler_hash"] is None
      and sanitize.public_part("error", "x.cxx: error: expected expression", "error: expected expression",
                               "x.cxx", T)["filler_hash"] is None,
      "a crash, or a diagnostic without names, has no filler")

# fillers_public: computed, published only with PUBLIC_FILLERS=1, else counted,
# logged for review, and blanked.
c = sqlite3.connect(":memory:")
c.execute("CREATE TABLE hl_signatures (sig_hash, fillers_public)")
c.executemany("INSERT INTO hl_signatures VALUES (?, ?)", [("a", '["139", null]'), ("b", '["139"]'), ("c", None)])
logged, st = [], collections.Counter()
os.environ.pop("PUBLIC_FILLERS", None)
sanitize.fillers_review(c, "hl_signatures", st, logged.append)
check(st["hl_signatures: fillers shown"] == 2 and st["hl_signatures: fillers withheld"] == 1
      and c.execute("SELECT count(fillers_public) FROM hl_signatures").fetchone()[0] == 0
      and len(logged) == 1 and '["139"]' in logged[0],
      f"PUBLIC_FILLERS off: counted, the values it would show logged, then blanked ({dict(st)}, {logged})")
c.execute("UPDATE hl_signatures SET fillers_public = '[\"7\"]'")
os.environ["PUBLIC_FILLERS"] = "1"
logged = []
sanitize.fillers_review(c, "hl_signatures", collections.Counter(), logged.append)
os.environ.pop("PUBLIC_FILLERS")
check(c.execute("SELECT count(fillers_public) FROM hl_signatures").fetchone()[0] == 3 and not logged,
      "PUBLIC_FILLERS=1: published, nothing logged")

shutil.rmtree(work)
print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
