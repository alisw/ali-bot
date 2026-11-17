"""sanitize.py: what a published ci-failures.db may not contain.

    python test/sanitize_test.py
"""
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

work = Path(tempfile.mkdtemp(prefix="sanitize-"))
db = work / "ci-failures.db"
c = sqlite3.connect(db)
c.executescript("""
CREATE TABLE ci_builds (build_id TEXT NOT NULL PRIMARY KEY, host TEXT, alloc TEXT, log_url TEXT);
CREATE TABLE ci_errors (build_id TEXT NOT NULL, sig_hash TEXT NOT NULL, host TEXT, text TEXT, sig_exact TEXT,
                        PRIMARY KEY (build_id, sig_hash));
CREATE TABLE ci_signatures (sig_hash TEXT NOT NULL PRIMARY KEY, sig_exact TEXT, embedding BLOB);
""")
c.executemany("INSERT INTO ci_builds VALUES (?, ?, ?, ?)", [
    ("b1", "alimetal07.cern.ch", A1, "https://ali-ci.cern.ch/alice-build-logs/x"),
    ("b2", "alibuildmac04.cern.ch", A2, None),
    ("b3", "aligpuci.cern.ch", None, None),          # a log without its allocation
])
c.executemany("INSERT INTO ci_errors VALUES (?, ?, ?, ?, ?)", [
    ("b1", "s1", "alimetal07.cern.ch",
     "ccdb path Users/m/meide/Tests/alimetal07.cern.ch/pid-1 failed; peer alibuildmac04 also; "
     "ROOT 6.36.10.4 at 188.184.86.225 and 127.0.0.1; ali-qcdb-test.cern.ch:8080 down; "
     "alice-ccdb.cern.ch fine; see https://alinomad.cern.ch/ui/allocations/" + A1, "x"),
    ("b2", "s1", "alibuildmac04.cern.ch", "token " + JWT + " on alibuildmac04.cern.ch", "y"),
    ("b3", "s2", "aligpuci.cern.ch", "no space left on aligpuci.cern.ch", "z"),
])
c.execute("INSERT INTO ci_signatures VALUES ('s1', 'error at alimetal07 and alibuildmac04.cern.ch', x'00ff')")
c.commit()
c.close()

stats = sanitize.sanitize_db(db)
print("stats:", stats)
c = sqlite3.connect(db)
hosts = dict(c.execute("SELECT build_id, host FROM ci_builds"))
check(hosts == {"b1": "alloc-5cab146f", "b2": "alloc-314d9697", "b3": None},
      f"ci_builds.host is the allocation, NULL without one ({hosts})")
check(dict(c.execute("SELECT build_id, host FROM ci_errors")) == hosts, "ci_errors.host follows its build")
t1 = c.execute("SELECT text FROM ci_errors WHERE build_id = 'b1'").fetchone()[0]
print("   ", t1)
check("Tests/alloc-5cab146f/pid-1" in t1, "a build's own machine becomes its allocation")
check("peer <host> also" in t1, "another builder (short name) becomes <host>")
check("<host>:8080 down" in t1, "another *.cern.ch name becomes <host>")
check("alice-ccdb.cern.ch fine" in t1 and "https://alinomad.cern.ch/ui/allocations/" + A1 in t1,
      "public services and the allocation link are kept")
check("at <ip> and 127.0.0.1" in t1 and "ROOT 6.36.10.4" in t1, "a CERN IP becomes <ip>; loopback and versions stay")
t2 = c.execute("SELECT text FROM ci_errors WHERE build_id = 'b2'").fetchone()[0]
check(JWT not in t2 and "<redacted>" in t2 and "alloc-314d9697" in t2, f"secrets are scrubbed again ({t2})")
check(stats.get("secret:jwt") == 1, "and reported by kind")
t3 = c.execute("SELECT text FROM ci_errors WHERE build_id = 'b3'").fetchone()[0]
check(t3 == "no space left on <host>", f"without an allocation, its own machine is <host> ({t3})")
sig = c.execute("SELECT sig_exact, embedding FROM ci_signatures").fetchone()
check(sig[0] == "error at <host> and <host>" and sig[1] == b"\x00\xff",
      "shared texts get <host>; blobs are left alone")
c.close()
raw = db.read_bytes()
left = [n for n in (b"alimetal07", b"alibuildmac04", b"aligpuci", b"ali-qcdb-test", b"188.184.86.225", JWT.encode())
        if n in raw]
check(not left, f"no trace in the file's bytes, free pages included ({left})")

again = work / "again.db"
shutil.copyfile(db, again)
check(sanitize.sanitize_db(again) == {}, "a sanitized DB (as a restored node has) is left as it is")

shutil.rmtree(work)
print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
