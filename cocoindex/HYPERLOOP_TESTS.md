# Hyperloop wagon tests in the ali-ci index (proposal, 2026-11-19)

Status: implemented as two flows of their own, not yet in the indexer's FLOWS:
`hyperloop` (hyperloop_flow.py, hyperloop.py: the errors, publishing
hyperloop.db) and `hyperloop-perf` (hyperloop_perf_flow.py, hlperf.py: the
resource metrics, publishing hyperloop-perf.db). They read different files of
the same test folders, so nothing is fetched twice and each is re-extracted
on its own; they share the id search and the walk (hyperloop.walk). Their
sizes, pass times and failures never touch the CI index. The MCP server
attaches them as "hl" and "hp". Fetched over plain HTTP: Python's TLS is
refused by alimonitor (hyperloop.BASE). test/hyperloop_test.py,
test/hlperf_test.py; cif_test.py runs the five flows, mcp_test.py the tools.

## What it would answer

The same questions the CI index answers for pull requests, for Hyperloop
wagon tests:

- Is this test failure my wagon's, the O2Physics tag's, or the input's?
  (the same error in other wagons' tests on the same tag and dataset points
  away from the wagon, as `other_prs_3d` does for a PR)
- Has this error been seen before, in other tests or in CI? Since which tag?
- Since when does wagon N fail, and on which datasets?

## The source

Every test has a public folder, no login needed:

    https://alimonitor.cern.ch/train-workdir/tests/<id // 10000, 4 digits>/<id, 8 digits>/

e.g. `tests/0080/00809001/`. HTTPS verifies with the jobs' existing CA bundle
(`cern-ca-bundle.pem`: the certificate is from the CERN Grid Certification
Authority); plain HTTP also answers. The folder of a test holds:

| file | size (00809001) | use |
|---|---|---|
| `status.json` | 3 kB | exit codes (`test_exitcode`, `merge_exitcode`: `done` / `failed`), wall time, end time, cores, input and output sizes, and per DPL device CPU seconds and PSS |
| `full_config.json` | 45 kB | identity: `train_id` (= the test id), `package_tag` (`VO_ALICE@O2Physics::daily-20261118-1200-1`), `inputdata` (dataset), `workflows[]` with `wagon_id`, `subwagon_id`, `workflow_name` (the O2Physics executable) and its configuration |
| `stdout.log` | 171 kB | the DPL run: device lines `[pid:device]: [time][ERROR] ...`, then the test summary |
| `stderr.log`, `merge.log`, `download.log` | 0.1, 52, 3 kB | the rest of the run |
| `AO2D.root`, `AnalysisResults.root`, `performanceMetrics*.json`, `profile.linux-perf.txt`, `merge_test*/` | up to 165 MB | not read |

No user name is in the folder: wagon names and owners live in Hyperloop.

## Scale

- Test ids run to ~809,000; the `0080/` bucket alone holds 9,080 tests.
- About 1,000 tests a day on weekdays, 300-400 at weekends (folder dates,
  10-19 November: 1,097, 864, 1,091, 1,382, 397, 293, 1,091, 1,027, 982).
- 6 of 40 recent tests failed (15%); logs are ~170 kB (median), 240 kB at most.
- Following new tests: ~45 tests an hour, 4 small files each, ~8 MB an hour.
  Backfilling 8 weeks (the CI log bucket's window): ~45,000 tests, ~8 GB of logs.

Measured live on 2026-11-19, from a Nomad batch job in meyrin (10 minutes, the
last 2 days, 4 requests/s at most, plain HTTP): 2,171 tests listed in 2 buckets;
747 read (146 failed, 20%) with 2,392 requests and 78 MB (gzip), i.e. 1.25 tests
a second; 5.2 MB of cache for them; rows() on all 747 without an exception. So
the 8-week backfill (~50,000 tests) is ~11 hours of fetching: ~45 passes at
HL_BUDGET_S=900, about 2 days. The first error of 78 of the 146 failed tests
was one crash (boost::unordered_map<>::at < _Sp_counted_base<>::_M_release <
o2::f...): the kind of pattern ci_hyperloop surfaces.

## How it fits

A fourth flow, `hyperloop`, next to `ci-failures`, built from the same parts but
sharing none of its files:

1. **Discovery.** Test ids are consecutive and a test runs once, so no listing
   is read: the newest id comes from status codes alone (`newest_id`: double
   the step from the newest id cached until a folder is missing, then bisect;
   ~12 HEAD requests for an hour of tests), and the walk goes down from it,
   reading each test not cached yet, once. A folder without `status.json` yet
   is a running test: kept pending, asked again each pass, given up on a day
   after it was first seen. The id below which alimonitor keeps nothing is
   remembered (`hl_state.floor`) for a week, then probed again: a hole in the
   ids mistaken for it cannot stop the backfill for good. The flow's cache (`state/alimonitor/tests.db`,
   in its state tarball) remembers what was read.
2. **Extraction.** `logscan` on `stdout.log` and `merge.log` of failed tests;
   the device of each error comes from its line, its crash signature or the
   driver's own report. Fingerprints come from `srctemplates.py` at publish.
3. **Tables** (`hyperloop.db`): `hl_tests`, `hl_test_wagons`, `hl_metrics` (CPU
   seconds and PSS per DPL device; CCDB counters per workflow, device `''`),
   `hl_errors`, and `hl_signatures` with the flow's own embeddings (an error
   also seen in CI is embedded by both flows: same text, same vector).
4. **Tools.** `ci_hyperloop(test_id | wagon_id)`; `ci_signature` and
   `search_ci_failures` report Hyperloop occurrences next to CI ones.
5. **Publishing.** `sanitize.sanitize_hl_db`, with its own publish list: errors
   by hash and fingerprint, no log text, a grid user's login in a dataset path
   hidden. Pointers into the public logs (`<folder>/stdout.log#L<line>`).

Politeness: alimonitor is a shared service. Requests serialised, at most
HL_RATE_PER_S a second, gzip, only the small files, and HL_BUDGET_S a pass.

## Size

hyperloop.db is published compact, and the MCP server's reader adds views
(reader.INDEXES) that give the readable shape back under the names tools and
`sql` users query: `hl_tests`, `hl_test_wagons`, `hl_metrics`, `hl_errors`.
The stored tables (`hl_*_data`, `hl_names`, `hl_meta`):

- names as ids: workflow and device names in `hl_names` (47 bits of their
  SHA-256, so every test agrees on them without a counter);
- whole numbers: PSS in kB, CPU in ms, counts and sizes as integers (a float
  is always 8 bytes in SQLite);
- no copies: a test's end time, tag and state only in `hl_tests_data`;
- no URLs: folder and log URLs from the test id and `hl_meta`'s base.

Measured on the fixtures (cif_test, same 3 tests), payload with the
primary-key indexes: metrics 5,450 -> 1,502 bytes (-72%), wagons 2,603 ->
1,137, tests 767 -> 461; `hl_names` adds 1,062, shared by every test.
Measured live (2026-11-19, Nomad job, the newest 999 tests, 209 failed; the
flow's exact tables, real rows, a random vector per signature): 4.9 MB, 4.9 kB
a test (passed 2.7 kB, failed 13.5 kB); the real DB over the estimate 1.04
(newer half 1.06, older half 1.05, passed only 0.92, failed only 1.18: stable
in time, and the mix of failures moves it little), so DEFAULT_SCALE is 1.1.
Bytes: signatures 39%, metrics with their key index 31%, errors 15%, wagons
10%. Published 0.88x the flow's DB, the MCP copy with indexes and views 1.02x,
the cache 8.6 kB a test (not budgeted). So 100 MB holds ~20,000 tests, ~20
days at 1,000 a day (signatures repeat: further in practice). Fetching: 112 kB
a test, 805 s for 1,000 (performanceMetrics_processed.json most of it, kept
for what else it holds); a pass over 999 stored estimates 24 ms. Since then
error rows hold no text (it is in hl_signatures once; publishing copies each
row's fingerprint from its signature): -79% of their bytes on the fixtures.
Empty metric rows are kept: there are few (1 in 32), and a workflow's
all-zero CCDB counters say "no CCDB requests", which is not "unknown".

## Retention

- **hyperloop:** the detailed tests (tests, wagons, per-device metrics,
  errors) within HL_MAX_DB_MB (20 MB, about a week: ~2.4 kB of those rows a
  test, ~1,000 tests a day), measured on those tables only (dbstat). A daily
  summary (`hl_error_days`: per day and error, the tests, distinct wagons and
  tags; `hl_days`: tests and failures per day) is recorded in the cache when a
  test is read and kept HL_SUMMARY_DAYS (180), with the test ids behind each
  count (`test_ids`: each leads back to its folder, so the details can be
  rebuilt while alimonitor keeps it; ~13 kB a day as a JSON list, against
  ~20 MB over 180 days as a table: the reader's view `hl_error_tests(day,
  sig_hash, test_id)` gives them as rows at no stored cost) and the signatures it names
  (`hl_signatures`, embedded): `ci_signature` and `search_ci_failures` report
  months of Hyperloop history from it.
- **hyperloop-perf:** per test and workflow, statistics of the ~5 s samples of
  performanceMetrics_processed.json (O2 Monitoring's ProcessMonitor: PSS and
  private dirty memory mean, median, p95, max and growth per minute; RSS and
  virtual max; CPU total, CPU % median and p95; retired instructions when
  present; CCDB counters; output bytes and the 5 largest output tables), and
  the whole train's (input processed, time frames, CCDB time). ~600 bytes of
  rows a test (fixtures); HP_MAX_DB_MB (100) holds months.

What is kept is watched, not budgeted, beyond the detailed window: gauges
`cocoindex_hl_db_bytes{part=detailed|summary|signatures|other}`,
`cocoindex_hl_cache_bytes`, `cocoindex_hl_summary_{days,tests,pairs,signatures}`,
`cocoindex_hp_db_bytes`, `cocoindex_hp_cache_bytes` (Grafana: "Hyperloop: what
is kept", "Hyperloop: daily summary"). If the summary grows too much: a budget
of its own, or at most N test ids per day and error (the counts stay exact).

## Decided (ktf, 2026-11-19)

1. **Backfill 8 weeks** (~45,000 tests), then follow new tests.
2. **Logs of failed tests only.** `status.json` of every test, failed or not:
   3 kB each, and the place the CPU and memory numbers come from.
3. **Wagon ids only.** No names or owners, no Hyperloop credential.
4. **CPU and memory are in:** per test and per DPL device, from `status.json`
   (`device_metrics`: CPU seconds, PSS), keyed by wagon and O2Physics tag, so
   "which tag made wagon N's memory grow" is a query. Worth reading as well:
   `performanceMetrics_processed.json` (1.2 MB for 00809001) has, per device,
   the `wagon_id`, CPU and memory, and the CCDB cache hits, misses and
   failures: whether it is reading a successful test's 1.2 MB is to be measured.
5. **Profiles: next iteration.** What was found: `profile.linux-perf.txt` is raw
   `perf script` output (one sample per block: process, pid, time, cycles, then
   the call stack, frames `addr symbol+off (library)`), 170 MB for 00809001.
   The server answers range requests (`Accept-Ranges: bytes`, 206) and gzips
   on request (`Content-Encoding: gzip`). A top 10 needs either the whole file
   (~1,000 a day: too much to stream from alimonitor without asking) or an
   estimate from a few random byte ranges, parsing the whole samples inside
   each and weighting by cycles; how many ranges give a stable top 10 is the
   first thing to measure.
6. **A flow of its own** (`hyperloop`, `hyperloop.db`), not tables inside
   ci-failures: the CI snapshot, pass and state stay as they are. One tool of
   its own (`ci_hyperloop`); `ci_signature` and `search_ci_failures` cover both.
7. **Bounded by size** (HL_MAX_DB_MB), not by a number of days: the index
   reaches back as far as the budget allows. How it holds:
   - newest first: new tests always get the budget; the oldest are dropped;
   - nothing is reserved for incoming tests: only the tests in the window are
     counted (hyperloop.est_bytes: their rows, each new signature's embedding
     once), scaled by the real DB's measure (pages in use over the estimate
     of what it holds; 2 until 200 tests are indexed; 2.13 on the fixtures);
   - space is freed in the same pass: dropped tests leave the cache before the
     flow plans its rows, so CocoIndex deletes them, and the published copy is
     compacted (VACUUM INTO);
   - no flapping: once the budget is filled nothing older is fetched, but tests
     already cached stay up to 5% beyond it (SLACK), so a small change of the
     scale does not drop the oldest one pass and fetch them again the next.
   - cheap: each test's estimate is stored with it in the cache when it is
     read (rows aside, its signatures as {sig_hash: bytes}, so one shared by
     tests is still counted once per walk), so a pass over a full window is
     a lookup per test, not a rows() rebuild.
   HL_MAX_TESTS (100,000) is only a ceiling.

8. **Compact rows** (see Size): names as ids, whole numbers, no copies, no
   URLs; the readers' views keep the queries as they were.
9. **No listings** (ktf: test ids are consecutive, a test runs once): the
   newest id by bisection on status codes; no HTML parsed, no folder times.

10. **Two flows** (ktf): errors and resource metrics apart, so either can be
    re-extracted on its own; per workflow statistics (mean, median, p95, max)
    of every memory counter, for memory and CPU optimisation by wagon.
11. **A week of detailed errors**, then a daily summary (ktf).

## Open decisions

1. **alimonitor:** tell its admins about the crawler, and agree a request rate.
