# cocoindex on Nomad, v2: SQLite outputs, S3 snapshots

## Architecture

One Nomad **service** job, `cocoindex` (`cocoindex.nomad`), one allocation, three tasks:

- `proxy`: security-proxy sidecar with three routes. `aigw` is for embeddings. `github` is GraphQL for the prs flow, and REST status history plus GraphQL PR files for the ci-failures flow. `s3` is an allowlist of two buckets: `alice-cocoindex` for snapshots, and `alice-build-logs`, which the ci-failures flow only reads.
- `bootstrap`: sidecar that pushes the grid cert, the aigw key, the GitHub PAT and the S3 keys from Vault into the proxy. It pushes again whenever the proxy restarts.
- `index`: `registry.cern.ch/alisw/cocoindex` (alisw/docks `cocoindex/`, pinned by digest; the MCP job uses the same one). Python 3.12, git, and the hash-locked venv of `requirements-linux.lock` in `/opt/cocoindex/venv`; the code ships as jobspec templates. It runs `runner.sh` as uid 1000 in group 8485, which gives it access to the agent socket.

All state lives in the sticky `ephemeral_disk` under `/alloc/data/cocoindex` (`sticky = true`, `migrate = false`). The job has no host volume and no Postgres.

```
flows/commits/state/       COCOINDEX_DB (LMDB, mdb/data.mdb)   } one pair per flow,
flows/commits/commits.db   output, SQLite + sqlite-vec          } always moved together
flows/prs/state/           LMDB + github/fetch.db (GitHub fetch cache) } same rule; the
flows/prs/prs.db           output, SQLite + sqlite-vec                 } cache is in the state tar
flows/ci-failures/state/   LMDB + s3/logs.db (extraction cache of every build log ever seen) } same rule
flows/ci-failures/ci-failures.db   output, SQLite + sqlite-vec                                 }
mirrors/<owner>__<repo>.git   bare, branches + refs/pull/<n>/head of indexed unmerged PRs, --filter=blob:none
published.json   snap/ (scratch)
```

## Data flow (runner.sh)

1. **Start.** For each flow in `FLOWS` that has no local state+db pair, `s3snap.py restore` fetches **both** from the run that `latest.json` names. Before a file is used, its size and sha256 are checked against the manifest. The pair is extracted into `flows/F.restoring/` and put in place with one `rename()`. If only half a pair is on disk, it is moved aside and the flow is restored.
2. **Pass.** The runner resolves the proxy port and gate tokens, then fetches the mirrors (`git fetch --prune`, then `git gc --auto`). It then runs `cocoindex update -f <flow>.py` once per flow, in an order shuffled every pass (`FLOW_ORDER=random`; `fixed` keeps the order of `FLOWS`). No flow reads another's output during a pass, and the shuffle means a flow that uses up its PASS_TIMEOUT (commits and prs, while the embedder is slow) does not always go first, ahead of the GitHub budget and the publish. Each run has its own `timeout --signal=INT PASS_TIMEOUT` (50m), after which it is killed.
3. **Snapshot.** Nothing is running at this point. For each flow:
   - copy the db with `VACUUM INTO snap/<db>`, never `cp`;
   - tar the LMDB dir without `lock.mdb` and compress it with zstd into `state-F.tar.zst`;
   - upload everything to `s3://<bucket>/cocoindex/<UTC run id>/`;
   - upload `<run id>/manifest.json`;
   - **last**, overwrite `cocoindex/latest.json` with the same manifest.

   The snapshot is skipped when no db's *content* hash has changed. The content hash is the sha256 of the `VACUUM INTO` copy with the SQLite header counters zeroed: reopening a restored db bumps the schema cookie once, and that alone must not count as a change.
4. **Prune** (the job does it, not an S3 lifecycle rule). Keep the newest `SNAPSHOT_KEEP` (12) run ids, plus whichever run `latest.json` names. Then sleep `PASS_SLEEP` (1h). The runner traps TERM and forwards INT to cocoindex.

**Why the job prunes rather than a lifecycle rule:** CERN S3 lifecycle rules expire objects by age and cannot express "keep the last N". If the job stalled for longer than the expiry (aigw down, cluster maintenance), the rule would delete the snapshot that `latest.json` points to, leaving nothing to restore. Lifecycle rules also need bucket-owner credentials, which never go in the proxy, so a human would have to apply them. An optional lifecycle rule that only aborts stale multipart uploads is harmless and could be added by hand.

## Why state and db must come from the same run

CocoIndex memoizes each component against its LMDB state and assumes the target already holds the rows that state recorded. A memo hit skips the component completely, so its rows are never declared again. Two consequences:

- **State newer than db:** rows are missing for good.
- **State older than db:** orphan rows are never deleted.

This was demonstrated locally. Restoring a state with a db that lacked one month (14 commits) gave `process_month 6 unchanged` and 0 embeddings, and the 14 rows stayed missing. The next publish then shipped that broken pair as `latest`. Restore therefore takes both halves from one run id or neither, and the runner refuses to start empty when a snapshot exists but cannot be read: it exits and Nomad retries with a delay. `RESTORE=skip` is the explicit override.

## Failure modes and recovery

| Failure | What happens |
|---|---|
| aigw down or slow | `LiteLLMEmbedder` retries fast failures inside `EMBED_TIMEOUT_S` (60 s; a slow request is not retried, its component fails and the next pass redoes it). `PASS_TIMEOUT` bounds the pass (the earlier hang ran for more than 1.5h). Memoized progress is kept and the next pass resumes. With 300 s, a slow aigw parked all 16 component slots for a whole pass (2026-11-17). |
| Pass killed (timeout, TERM, OOM) | LMDB and SQLite are each crash-safe, and cocoindex reconciles on the next pass. Tested: a copy of v1's interrupted `run/` reopened at another path, added the missing 631 commits, and the following pass was a no-op. Note that cocoindex exits **0** after INT, so `pass_exit` in the manifest does not reveal an interruption. |
| Node lost / job purged | A new allocation on an empty disk restores the last pair. It loses at most the work since the last published snapshot. |
| Snapshot upload fails | Only logged. The local pair is intact and the next pass publishes. `latest.json` still names a complete older run. |
| Two allocations at once (disconnected node) | Each run id is self-consistent and `latest.json` names a complete run, so readers are safe. The cost is duplicate uploads. Prune never deletes the run `latest.json` names. |
| Corrupt or mismatched pair (or bad snapshot) | `cocoindex update --reset` on the flow, or start with `RESTORE=skip` on an empty disk. Cost: **a full re-embed** (measured: `--reset` re-sent all 330 of 330 texts in the test). `--full-reprocess` costs the same. |

## Sizes (estimates from measured per-row sizes × GitHub commit counts)

- **Commits:** about 53k on the indexed branches (AliceO2 dev 23.4k, O2Physics 17.1k, alidist 7.0k, O2DPG 3.3k, ali-bot 2.5k).
- **Diff-eligible commits:** about 13.7k since 2025-01-01.
- **Measured row sizes:** a `commits` row is about 3.2 KB and a `diff_chunks` row about 4.7 KB (768 float32 is 3 KB of that).
- **commits.db:** about 0.4–0.6 GB. **LMDB state:** about 1.5× the db. **One snapshot:** about 1–1.3 GB, because vectors do not compress. Keeping 12 runs is about 15 GB in the bucket.
- **Bootstrap volume:** about 85–110k texts, or about 1.5k requests at 64 texts each, sent to aigw.
- **Blobless mirrors:** every `git show --numstat`/`-p` lazily fetches that commit's blobs. This was measured at about 0.5 s and one new pack per call. The bootstrap therefore makes about 67k small GitHub fetches, and the mirror converges to the full repository size (about 350 MB on GitHub) anyway. `git gc --auto` folds the packs after each pass.

## Throughput

- **Concurrency limits:** `COCOINDEX_MAX_INFLIGHT_COMPONENTS` (default 16) and `EMBED_MAX_INFLIGHT_REQUESTS` (default 4). Neither setting is in a memo key, so both can be tuned without invalidating cached vectors.
- **The deadline includes the wait for a request permit.** `LiteLLMEmbedder` takes its in-flight permit inside the `EMBED_TIMEOUT_S` deadline scope, so a batch queued behind the 4 permits spends part of its 60 s waiting. On 2026-11-18, 46% of commits' and 27% of prs' requests timed out with 16 components. With 6 components (09:17–09:47) 44% of commits' requests still timed out and throughput fell to 747 texts in 30 min, while the idle probe averaged 19 s (up to 57 s): aigw itself was slow, and the queue was not the main cause. The job runs 16 again.
- **Task prefixes** (`embedtext.py`, decided 2026-11-17). embeddinggemma was trained with them: every stored text is embedded as `title: none | text: …` and every search as `task: search result | query: …`. The one exception is `ci_signature`'s nearest-error lookup, which compares a pasted error with stored ones as a document. On 400 O2 commits (query = subject, document = the rest of the message) MRR went from 0.791 bare to 0.810, R@1 from 0.710 to 0.738. The flows and `reader.py` import the strings from that one module; changing them changes every vector, so the index is then rebuilt from scratch. The first deployment was restarted from an empty state for exactly this.
- **Endpoint and key** come only from the environment (`OPENAI_API_BASE`/`OPENAI_API_KEY`). `OPENAI_BASE_URL` is unset because litellm would prefer it.
- **How to size the defaults:** watch the first pass's texts/s and any 429/5xx, then raise the requests setting until aigw degrades.
- **Measured 2026-11-16** (from the Mac, `embeddinggemma-300m`, texts of ~900 chars): one request of 64 texts took 17 s (3.7 texts/s); 4 requests in parallel gave 7.7 texts/s, so aigw scales sublinearly. The bootstrap of commits plus PRs (~200k texts) is then about 7 h of embedding. With `PASS_TIMEOUT` 50m per flow and `PASS_SLEEP` 1h it spreads over roughly 5–8 passes, i.e. most of a day. The only other names tried (`qwen3-embedding-*`, `bge-m3`, `nomic-embed-text`, `text-embedding-3-small`) are not available to the key.

## The prs flow (`prs_flow.py`)

Pull requests and their human review discussion, for the same five repositories, into `prs.db`. It runs in the same pass as `commits` (`FLOWS=commits,prs`).

**Selection.** By the PR's `authorAssociation`: OWNER, MEMBER, COLLABORATOR and CONTRIBUTOR are indexed. FIRST_TIME_CONTRIBUTOR, FIRST_TIMER, NONE, MANNEQUIN and deleted accounts (`author: null`, or `ghost`) are skipped. PRs opened by bots (`__typename == "Bot"` or the list below) are recorded as `bot` and never looked at again. On recent PRs, 21–30 of 40 per repository were MEMBER and 3 of 40 in O2Physics were first-timers.

**Re-checking skipped authors.** GitHub evaluates `authorAssociation` when it is asked, so a first-timer becomes CONTRIBUTOR once a PR of theirs is merged, without their earlier PRs changing. Every pass therefore asks about one PR per distinct (repo, author) among the skipped ones, 100 per `nodes(ids:)` query at 1 point each. Once an author qualifies, all their skipped PRs in that repository are fetched again by id and indexed. Deleted accounts are never asked about. A skipped PR that *is* updated is simply picked up again by the incremental scan.

**Fetching.** GraphQL through the job's `github` route: the runner exports `GITHUB_GRAPHQL_URL=http://<proxy>/github/graphql` and the route's gate token as `GITHUB_TOKEN`, as ci-mcp and queue-metrics do. On a 401 (daily rotation) the flow asks `proxy-token.py` again. Each pass:
- per repository, `pullRequests(orderBy: UPDATED_AT DESC)`, 25 per page, with comments and reviews (first 50) and review threads (first 10, 20 comments each) nested; a connection that overflows is paged in afterwards. The scan stops at the last complete scan's newest `updatedAt` minus `PRS_OVERLAP_S` (15 min). PRs in the overlap whose `updatedAt` is unchanged are not processed again.
- The cursors are keyset (decoded: `cursor:v2:` + updatedAt + id), so the scan's `endCursor` is saved with each page, in the same transaction as the page's PRs. A scan stopped by `PRS_FETCH_BUDGET_S` (20 min), `PRS_MAX_PAGES`, a timeout or a crash resumes exactly there on the next pass. A PR updated meanwhile moves in front of the scan and is caught by the next one.
- Rate limits: `rateLimit { cost remaining resetAt }` is read on every reply. Below `PRS_RATE_RESERVE` points (default 500; 1500 in the job, since the token is `alibuild`'s, which the CI's claim workers list their work with) the flow sleeps until `resetAt`. On HTTP 403/429 with `retry-after` or `x-ratelimit-remaining: 0`, or on a `RATE_LIMITED` error, it waits for the reset. A wait that would outlast the budget ends the sync for this pass instead. 5xx (GitHub times out heavy queries) is retried with backoff.
- If GitHub is unreachable, the pass indexes what the cache already holds.

**Fetch cache.** The raw PR JSON, the per-repository scan state and the skipped list live in `state/github/fetch.db`, *inside* the LMDB state dir. They are therefore in the state tarball and restored with it. This is required, not a convenience: the flow mounts one component per cached PR, so a lost cache with a surviving state would unmount every PR, and cocoindex would delete all their rows. With a cache that is merely *older* than the db (it cannot be: they are in the same snapshot), the next scan would only fetch more. Tested: a fresh host restores the pair and its next pass sends one list request and no embeddings.

**What is embedded.** One memoized component per PR keyed by (repo, number), called with the sha256 of its cached JSON. An unchanged PR is a memo hit. A changed one declares all its rows again, so an edited or deleted comment disappears (tested). Unchanged texts inside it are themselves memo hits: the test's edit re-sent 1 of #1's 4 texts. Components are grouped by month of creation, like commits.
- `prs.embedding`: `"<repo>#<n>: <title>\n\n<body>"`. HTML comments (`<!-- ... -->`, which the PR templates are full of) are stripped and blank runs collapsed. The body is stored up to 20k chars; past the first ~5k, up to 3 more `body` chunks.
- `pr_chunks`, kind `comment` and `review`: human issue comments and non-empty review bodies (`[changes_requested]` etc.), each as `@login (date): text`. They are packed in time order into chunks of at most 4.4k chars, and a single comment longer than that (a pasted log) is cut. Kind `thread`: one chunk per review thread, headed `path:line`, with `path` and `line` columns. Minimized comments (spam, off-topic) are dropped. At most 30 discussion chunks per PR.
- **Bots**: `__typename == "Bot"`, plus `alibuild` (a *User*, MEMBER, posting CI error reports and "Auto-approving on behalf of"), `github-actions` (also the O2 linter's account: "O2 linter results"), `ALICE-CI-bot`, `dependabot`, plus `PRS_BOT_LOGINS`. Seen on recent PRs: `alibuild` 72 and `github-actions` 41 of the comments on 40 O2Physics PRs; no separate linter account.
- **Diffs** (`PRS_DIFFS=1`, on by default): only for open and closed-without-merging PRs updated since `DIFF_EMBED_SINCE`. Merged code is already in `diff_chunks`, and `commits.pr_number` links to it. Before indexing, the flow fetches `+refs/pull/<n>/head` into the existing mirror for each eligible PR whose head commit is missing (batched, `--no-write-fetch-head`). The runner's `fetch --prune` only prunes `refs/heads/*`, so those refs persist. The diff is `merge-base(base branch, head)..head`, chunked as in the commits flow (4 per file, data files skipped), at most 40 per PR. Whether the head is present is part of the component's arguments, so a failed fetch is retried on a later pass rather than memoized as "no diff".

**Output tables.**
- `prs(repo, number, title, author, author_association, state, draft, created_at, updated_at, closed_at, merged_at, base, head, head_sha, url, body, n_comments, embedding)`. `state` is `open`, `closed` (not merged) or `merged`.
- `pr_chunks(repo, number, kind, chunk, author, path, line, created_at, text, embedding)`.
- `pr_repos(repo, fetched_through, scan_in_progress, indexed, skipped, bots)`. `index_status` uses it to say when the bootstrap is still running. `indexed` counts the PRs selected for indexing after the GitHub sync, not those already embedded. The rows are declared in a child component of their own (`declare_repo_status`), mounted before the per-month work and waited for. Rows declared by `app_main` itself are only written when the whole update completes, and a run stopped at `PASS_TIMEOUT` never does: until 2026-11-18 `pr_repos` kept the first pass's values (no watermark, 0 indexed) for every repository.

The cocoindex SQLite target creates no secondary indexes, and they must not go into the indexer's live db: SQLite refuses `ALTER TABLE DROP COLUMN` on an indexed column, which a cocoindex schema change may issue. `reader.sync()` therefore adds them to its verified copy before the `rename()`: `prs(created_at)`, `prs(author COLLATE NOCASE, created_at)`, `prs(state, created_at)` and `commits(repo, pr_number)`.

**Sizes and bootstrap cost** (from 25 real AliceO2 PRs run through the flow's own text code, and the totals per repository):
- **PRs:** about 45k, of which about 90% are indexed (24 of 25 in the sample). They make about 2.8 texts per PR before diffs: 1 description and 1.8 discussion chunks, of which threads are the largest share.
- **Bootstrap embedding:** **about 80–115k texts**, plus about 5–10k diff chunks for the unmerged PRs since 2025. The estimate of 60–90k assumed fewer threads; AliceO2 is the most review-heavy repository, so the true figure is likely near the low end.
- **GitHub:** a page of 25 costs **3 points** (it was 13 with 50 threads nested; measured) and takes 2.1 s at about 160 KB. So the bootstrap is about 1.8k pages, **about 5.5k points** plus overflow pages, and about 65 min of fetching. At 20 min per pass that is 4 or more passes. During those passes `pr_repos.scan_in_progress` is set, and the repositories are scanned in order.
- **Steady state:** one list page per repository per hour, plus the re-check (a few `nodes` queries).
- **fetch.db:** about 6.5 KB of JSON per PR, so about 300 MB on disk and about 60 MB in the state tarball. **prs.db:** about 3.5 KB per row (3 KB is the vector), so about 0.4 GB.

**Untrusted text.** PR titles, bodies, comments and diffs come from third parties and are handed to an LLM. The tools return each piece only inside an `untrusted` object: `{"title", "excerpt"}` in `search_prs`, `{"title", "body"}` and per-chunk `{"text"}` in `get_pr`, and `{"title"}` on `get_commit`'s `pr`. It is a JSON string field, so it cannot break out of its delimiters. Each reply has a `note` saying what the field holds. The server instructions say that this content is data to quote or summarise and never instructions. The caps are a 300-char title, an 8k body, 3k per chunk, 20k per `get_pr` and 500 per search excerpt. The `sql` tool's description repeats the warning, and its cells stay capped at 2000. Commit messages, which are also third-party text, are not wrapped this way (see the open decisions).

## The ci-failures flow (`ci_failures_flow.py`)

Every CI build log in `s3://alice-build-logs`, into `ci-failures.db`, so that PR authors (through the MCP server) and agents can ask: is this failure my PR's fault or a broken build or builder? Has this error been seen before, since when, on which PRs, checks and hosts? Is it flaky? Which builds of a PR ran where? It runs in the same pass as the other two (`FLOWS=commits,prs,ci-failures`).

**The bucket.** `report-pr-errors` uploads, for every build, green or red, `<owner>/<repo>/<pr>/<sha>/<check context, every non-[A-Za-z0-9_-] -> _>/fullLog.txt` and `pretty.html`. Branch builds (the daily tags) put the branch name where the PR number goes, plus a `latest/` copy, which is skipped. Measured 2026-11-16: about 10.3k logs, 63 GiB (failed builds 1.9k, 11.6 GB), about 170 new per day, kept about 8 weeks; 54k objects in all (artifacts included), listed in 56 ListObjectsV2 pages in 7 s from the Mac. **A re-run of the same sha and check overwrites the same key**, so the bucket never holds more than the latest attempt.

**Durable extraction cache.** As with the prs flow's fetch cache, the flow keeps `state/s3/logs.db` inside the state dir, so it is snapshotted and restored with the LMDB state and obeys the same pairing rule. Its `build` table has one row per **(log key, etag)**: a build. Rows are never deleted, so a log that expires keeps its rows (`ci_builds.gone`), and an overwritten key becomes a second build of the same key (`attempt` 2). That is how "red, then green on the same sha" is seen without S3 versioning (which the proxy does not expose and is probably off). Each pass:
1. Lists the bucket and records which keys are gone.
2. For every new build, reads the **first 4 KB of `pretty.html`** (a ranged GET). This gives the exact verdict of that build (`<body class="succeeded|failed">`), the host, start and finish times (converted to UTC), and the allocation. It is 4 KB instead of a 5 MB log, so every build has a row and a verdict in the first pass. A `pretty.html` whose LastModified is more than 15 min away from the log's belongs to another upload, and is asked for again in the next pass.
3. Downloads logs that have not been extracted yet, failed (or unknown) builds first, newest first, within `CIF_FETCH_BUDGET_S` (20 min). Threads download (`CIF_FETCH_THREADS`) and a spawn-mode process pool extracts (`CIF_EXTRACT_PROCS`, 4): extraction is pure Python at 8–10 MB/s per core. Only the extraction is kept, never the log: the header (host, builder-local finish time, sha, PR, allocation), the aliBuild sections, the failed package, and at most `CIF_MAX_ERRORS` (40) distinct errors, ranked as ci-mcp ranks them.
4. Asks GitHub, best effort, within `CIF_GH_BUDGET_S` (10 min):
   - For each commit, the **REST status history** (`/commits/<sha>/statuses`, at most 5 pages). It is fetched again when a new build of that commit has been listed since. It gives the real check names and how many times a check ran and with what result. The CI re-posts "error" while a red check waits to be rechecked (95% of a busy commit's 500 statuses), so a *run* is a `pending` followed by its first final status, and the history is compacted before it is stored (10.4k statuses become 942 on 39 real commits, with the run counts unchanged).
   - For failed builds of PRs, the **PR's changed files** (GraphQL `files(first:100)`, 10 PRs per query, cached per (pr, sha)). The prs flow's fetch cache has no file lists, so nothing is reused from it.

**Verdict** (`state_source`): `pretty` (from that build's own `pretty.html`), else `github` (the final status posted within 15 min of the upload), else `inferred` (no-output or a fatal error means a failure), else `none`. The brief proposed GitHub first. `pretty.html` is exact per build attempt, survives overwriting, and costs one small GET; GitHub statuses are per (sha, context) and only the newest is cheap.

**Extraction** lives in `../ci-mcp/logscan.py`, shared with ci-mcp (see "Shared extraction" below). On each line, after aliBuild's prefix is stripped and `NOISE_RE` applied (plus the green ctest summary and bash xtrace `+ ...` lines, which are commands, not output), the first matching kind wins:
- **Infra kinds, first.** Evidence: the tails of all 1,595 non-empty failed logs and 301 no-output logs in the bucket on 2026-11-16.

  | kind | count | rule |
  |---|---|---|
  | `no-output` | 301 | under 2000 bytes and "No logs found" |
  | `gpu-unavailable` | 42 | the GPU backend could not be loaded |
  | `disk-full` | 33 | "No space left on device", errno 28 (18 on one macOS check) |
  | `missing-system-dep` | ~75 | a system package missing from the builder image (GLUT, GIF, LibLZMA, ...) |
  | `alidist-sync` | 29 | "FindO2GPU.cmake differs ... compared to alidist" |
  | `remote-cache` | 16 | recc "Blob absent" |
  | `killed` | 0 | OOM; kept from report-pr-errors |

  Network, DNS and git-fetch failures do not appear in `fullLog.txt`: report-pr-errors puts the fetch log only into `pretty.html`.
- **Then report-pr-errors' kinds** (lifted by AST): `cmake`, `test`, `fst-*`, `error`.

**Signatures**, two levels, each hashed (sha256, 16 hex):
- `sig_exact`: the error with these removed or normalised:
  - build-path prefixes, so `/sw/SOURCES/<pkg>/<arch-or-PR>/<n>/x.cxx`, `/Volumes/.../sw/SOURCES/...` and the relative `x.cxx` printed by code-check are one file;
  - install and build dirs, which become `<pkg>:`;
  - `:line:col` and `(line)`;
  - hex hashes, tmp paths, timestamps, pids, ports and `[pid:thread]`, ninja `[n/m]`, ctest numbers and durations, PR numbers, `*.cern.ch` host names, and `,-warnings-as-errors`.

  The first line is the signature, except for CMake errors, whose message is in the next lines.
- `sig_class`: the message template. The file path is removed, quoted names become `'_'` and numbers become `N`. Example: "error: C-style casts are discouraged; use static_cast [google-readability-casting]", or "error: unused variable '_' [-Wunused-variable]".

`file` and `package` come from the start of the error line (or "at <path>:<line>"). `in_pr_files` is 1 when the file is among the PR's changed files (exact or suffix match). It is 0 when it is not, or when the file belongs to another package or to an installed or build tree. It is NULL when that cannot be known: no file, no file list, or more than 100 changed files without a match.

**Crashes.** A crash reaches the line patterns only as its last words (DPL's "Workflow crashed - PID N (<device>)…", ROOT's "*** Break ***"), which say which process died but not where. `logscan.crashes()` reads the stack trace instead and indexes its fingerprint as an error of kind `crash` (serious, fatal): `sig_exact` is `crash in <device> (<executable>): f1 < f2 < …`, the top 5 named frames innermost first, and `sig_class` is `crash: f1 < f2 < …`, the same crash in any device. Addresses, pids, argument lists, template arguments (`<>`), library paths and versions are dropped, and so are the frames every crash shares: libc and the C++ runtime, FairLogger, FairMQ and libuv, DPL's run loop and backtrace helper, ROOT's signal handler and interpreter, `main`. A frame without a symbol counts as its library (`[libO2CCDB]`), once per run. Three formats: DPL on Linux (seen in the logs), DPL on macOS (`backtrace_symbols()`, from `BacktraceHelpers.cxx`) and gdb as ROOT's crash handler prints it (`etc/gdb-backtrace.sh`: the frames after `<signal handler called>` of the crashed thread). DPL prints a second trace when it aborts; it holds only skipped frames and yields no fingerprint. On two real aarch64/x86 logs: "crash in internal-dpl-ccdb-backend (o2-sim-digitizer-workflow): o2::ccdb::CcdbApi::initTGrid < CcdbApi::loadFileToMemory < …", the same hash for both crashes in that log. The reason (the device's last `[FATAL]`/`[ERROR]`) is its own error, as before.

**Pointers, not text.** Every error records `line`, the 1-based line of the log it was taken from (for a crash, its trace's first line). The index rows hold that and the build's `log_url`, never the log text: the extraction cache (private) keeps the scrubbed sample. `logscan.EXTRACT_VERSION` is stored per build in the cache (`extracted`), and a newer version re-reads the logs still in the bucket that an older one read, after the never-read ones; logs gone from the bucket keep what was extracted (their errors have `line` NULL). Version 2 (2026-11-17) added crashes and lines.

**Secrets.** Every stored text (each error with its body lines, and the header fields) goes through `logscan.scrub()` before it is signed, stored or embedded. It removes GitHub `ghp_/gho_/ghu_/ghs_/ghr_` and `github_pat_` tokens, GitLab `glpat-`, AWS `AKIA/ASIA` keys, `Authorization:`-style headers, `password=`/`token=`/`secret=`/`*_key=` values, PEM private key blocks (cut ones included), and URLs with userinfo. The real fixtures contain nothing the scrubber would change. Scrubbing the whole log first does the same job and cost a third of the extraction time on 10 MB logs.

**What is embedded.** Only signatures, one text per distinct signature (`"<kind>: <sig_exact>"`), and only when it was seen in a build that did not pass. Never whole logs, never per build. Signatures are grouped into 256 components by hash prefix, so a pass with one new signature re-runs one group, and inside it everything except the new one is a memo hit. Builds and errors carry no vectors.

**Components.** One per build, keyed by build id. Builds are grouped by (repo, upload month) like commits, and each is called with the fingerprint of everything its rows are made of: the extraction, `pretty.html`, the commit's status history, the PR file list, the learnt check name, the attempt number, current/gone, and `ROWS_VERSION`. Bump `ROWS_VERSION` when `assemble()` changes; that re-declares all rows but downloads and embeds nothing. The check name is learnt globally from every fetched status history (check_dir → context), because `_` → `/` cannot be undone by guessing ("build_O2_fullCI_slc9" is "build/O2/fullCI_slc9").

**Output tables** (`check` became `check_name`: CHECK is an SQL keyword; errors also carry `rank`, `fatal`, `package`, `section`; embeddings moved into their own table):
- `ci_builds(build_id, repo, pr, branch, sha, check_name, check_dir, host, alloc, alidist, started_at, finished_at, uploaded_at, attempt, current, gone, state, state_source, gh_state, gh_runs, gh_failures, failed_package, packages, n_errors, n_distinct, infra_kinds, errors_with_file, errors_in_pr_files, no_output, extracted, log_bytes, log_key, log_url)`, where `log_url = https://ali-ci.cern.ch/alice-build-logs/<key>`. `alidist` is the alidist commit the build used, from the header line `Built with alidist <sha>` that `report-pr-errors` writes since 2026-11-16 (decided that day); NULL for older logs.
- `ci_errors(build_id, sig_hash, rank, repo, pr, sha, check_name, host, uploaded_at, state, kind, infra, fatal, class_hash, sig_exact, sig_class, file, package, section, in_pr_files, count, line)`. It is denormalised, so "same signature, other PRs, ±3 days" is a single indexed query.
- `ci_signatures(sig_hash, class_hash, kind, infra, sig_exact, sig_class, file, embedding)`.
- `ci_repos(repo, logs_in_bucket, builds, extracted, pending, oldest, newest)`, which shows bootstrap progress.

They join to `prs` on `(repo, pr = number)` and to `commits` on `(repo, sha)`. Readers add `ci_errors(sig_hash, uploaded_at)`, `ci_errors(class_hash)`, `ci_errors(repo, pr)`, `ci_builds(repo, pr, uploaded_at)` and `ci_builds(repo, sha, check_dir)` to their copy.

**Throughput and sizes, measured.** From the Mac, through the proxy, on 2026-11-16:
- **Listing:** 7 s and 19 MB of XML per pass.
- **pretty.html heads:** 81 in 0.6 s.
- **Logs:** 30 logs (191 MB) in 15.9 s, i.e. 12 MB/s with 4 download threads and 4 extraction processes. Extraction alone ran at 37 MB/s on 4 cores and 9.8 MB/s on one; downloads alone at 26 MB/s with 8 threads.
- **GitHub:** about 0.55 s per request, about 2.7 REST pages per commit before compaction.
- **aigw:** about 8 texts/s (67 signatures).
- **Sizes:** 30 extracted builds gave 116 error rows and 67 signatures. The cache holds about 2.4 KB of errors per extracted build and about 4 KB of compacted status history per commit.

**Bootstrap estimate** (10.3k logs, 63 GiB):
- **pretty.html:** about 2 min. Every build has its verdict and host in the first pass.
- **Logs:** at 12–25 MB/s, 45–90 min of downloading, so 3–5 passes at 20 min. The 11.6 GB of failed logs come first and take the first pass.
- **GitHub:** about 2.4k status histories at about 1.5 s each (60 min), so about 6 passes at 10 min. Changed files take about 100 queries.
- **Embedding:** an estimated 5–10k distinct failure signatures, 10–20 min at 8 texts/s.
- **Total:** about 5–8 passes, i.e. half a day with the hourly sleep.
- **Steady state:** about 7 logs/h (about 30 MB), a dozen GitHub requests and a handful of new signatures per pass.

**Failure modes.**

| Failure | What happens |
|---|---|
| S3 unreachable or the token stale | 401/403 re-resolve the gate token (`PROXY_SOCK`/`PROXY_TOKEN`). Otherwise sync is skipped and the cached builds are indexed. |
| Log overwritten while downloading | The etag of the download must match the listed one, or the build waits for the next pass. The new etag is a new build anyway. |
| Log expired before it was downloaded | The row keeps its `pretty.html` verdict and host, with `extracted=0`. |
| GitHub down or rate-limited | Sync ends for this pass. Rows keep `check_name` as a guess and `in_pr_files` NULL until it answers. |
| Lost extraction cache with a surviving state | The same as the prs flow: never separate them. History older than the bucket's 8 weeks would be gone. |
| aigw down | The same as the other flows. Builds and errors are still written; signatures wait. |
| A rebuild within one pass interval | The red attempt is never seen in the bucket, but its "pending → error" run is still in the GitHub status history (`gh_runs`, `gh_failures`). |

**Untrusted text.** Log text is produced by the code under test. The served index holds none; the MCP tools return the `fingerprint` (crash or compiler template, else NULL) only under `untrusted`, with a note, as for PR text, and point at the log instead: `location` is `<log_url>#L<line>` (the fragment is for the reader; the log needs CERN SSO, `alice-member`). File paths are outside it: the path regex admits only `[A-Za-z0-9_.+/-]`.

**Shared extraction (`../ci-mcp/logscan.py`).** ci-mcp's per-line extraction moved here unchanged: the AST loader of report-pr-errors' patterns, `NOISE_RE`, `FATAL_RE`, `clip`, `with_body`, `strip_alibuild_prefix`, the scan loop (`scan_lines`) and the ranking (`severity`). The new pieces are the infra rules, header and section parsing, signatures, the scrubber and `extract_log()`. ci-mcp keeps re-exporting the old names. Its output was compared before and after the move on 60 real logs, and all 60 were byte-identical. It lives next to ci-mcp, not in `ci/`, because `ci/` is deployed onto the builders, which never run it. The indexer's jobspec ships `logscan.py` and `report-pr-errors` with `file("../...")`, and the MCP jobspec ships `logscan.py`, which `ci_signature` needs to normalise pasted error text.

**Tests.**
- **`test/cif_test.py <workdir>`** (61 checks). `test/fake_s3.py` serves ListObjectsV2 with forced paging, ranged GETs, ETags and mtimes, logs every request, and refuses writes. Its bucket holds 8 real logs, trimmed (`test/fixtures/ci-logs/`, 104 KB), plus synthetic ones. `fake_github.py` grew the REST status history and `CIFFiles`. Six runner passes with `FLOWS=commits,prs,ci-failures`:
  1. A bootstrap cut at 3 logs: every build has its verdict from a ranged `pretty.html` GET, failed logs come first, and `ci_repos` shows 7 pending.
  2. Resume: kinds, signatures, `in_pr_files`, check names, UTC times, no-output, disk-full, the GLUT error shared by two PRs, planted secrets absent from both databases and from the embedded texts, one embedding per distinct failure signature, and none for green-only ones.
  3. Changes: a red build re-run green on the same key (attempt 1 kept, attempt 2 current, the status history shows 2 runs with 1 red), a log deleted (rows kept, `gone`), an etag change (re-extracted as attempt 2), and a third PR with the GLUT error. Only these 3 logs are downloaded and only new signatures embedded.
  4. Nothing changed: listing only, no embeddings, no snapshot.
  5. A fresh host restores: nothing downloaded, nothing embedded, the same rows.
  6. A log extracted by an older `EXTRACT_VERSION`: only it is downloaded again, nothing embedded, the same rows.

  Pass 2 also checks a DPL crash's fingerprint (indexed and embedded), that every error's `line` points at that error in its log, and that `ci_errors` has no text column.
- **`test/mcp_test.py`** on that store: 188 checks (the 114 of the prs store plus the CI checks). **`sso_test.py`**: 89 on the prs store, 113 on the cif store. **ci-mcp:** `tests/test_logscan.py` (scrubber per secret kind, signatures, header, real fixtures) brings it to 30 unit tests.


## What a published ci-failures.db contains (`sanitize.py`)

Decided 2026-11-17. The CI logs come from a private bucket and name the builders; the commits and PRs are public GitHub data. So `s3snap.publish()` runs `sanitize.sanitize_db()` on the VACUUM INTO copy of `ci-failures.db` it uploads, never on the flow's own database:

- **Secrets, again.** Every text value goes through `logscan.scrub_report()`. The flow already scrubs what it extracts, but its extraction cache can hold text scrubbed by older patterns. A hit is logged as `WARNING: secret-like text redacted before publishing`, by kind, never by value: it means a pattern was missing at extraction. `logscan` gained JWT, Google, Slack, `sk-` and PGP private-key patterns at the same time. Measured on the first real snapshot (11,659 errors): no secret-like text at all, before or after.
- **Machine names.** The `host` columns hold `alloc-<first 8 of the Nomad allocation>` (NULL when the log recorded none: 1 of 10,400 builds). In a build's texts its own machine, by full and short name, becomes that `alloc-<id>`; any other builder the DB knows of, any other `*.cern.ch` name, and CERN or private IPv4 addresses become `<host>` / `<ip>`. Public services stay (`KEEP_HOSTS`: alice-ccdb, alimonitor, alien, s3, gitlab, auth, ali-ci, and alinomad, whose allocation links are what replaces the names). An operator finds the machine from the allocation in Nomad. On the first snapshot this replaced 478 own-machine mentions, 30 other builders, 220 other cern.ch names and 7 IPs; no builder name is left in the file's bytes.
- **Deny by default.** Only the tables and columns in `sanitize.PUBLISHED` leave the job: everything else (a column a later change adds, a table, view, trigger or index nobody listed) is dropped, then the copy is checked to hold exactly that list, or the publish fails. Listed: public material (repos, PRs, shas, GitHub check names and states), CI metadata (times, counts, packages, `alloc-<id>`), errors by hash (`sig_hash`, `class_hash`), a readable `fingerprint` where its text is public, file paths that pass `logscan.public_path` (none under a home, tmp or build directory), the embeddings (searched on the server, never returned: `sql` prints vectors as placeholders), and pointers (`log_url`, `line`). Not listed: log text, signature text (`sig_exact`, `sig_class`), the full allocation id.
- **Fingerprints, not messages** (decided 2026-11-17). `logscan.public_fingerprint()` keeps readable text only where it comes from public sources alone: a crash's fingerprint (device, executable, function names), and the template (`sig_class`, names `'_'`, numbers `N`) of a compiler or clang-tidy diagnostic on a public file, e.g. "error: unused variable '_' [-Wunused-variable]". Boost.Test's "error: in '_': check …" is runtime output and does not count. Everything else (test and runtime output, CMake, infra) is hash-only: that free text is where user names, CCDB paths and internal URLs show up. On this morning's snapshot: 2,540 of 3,642 signatures keep a template, 1,102 are hash-only, 11 file paths are withheld, and none of the 3,639 signature texts is left in the file. Search still works (the server embeds the query and compares), and `ci_signature` still takes a pasted error line (the server hashes it); what a hash-only error says is read in the log, at its `location`. Every MCP tool, `sql` included, can only return what this file holds, so this is where "only public material and fingerprints" is enforced. On this morning's snapshot it dropped `ci_errors.text` and `ci_builds.alloc`; none of 265 sampled error bodies is left in the file's bytes.
- **Templates from the code that printed the message** (`srctemplates.py`, decided 2026-11-17). A runtime message is matched to the statement in public code that prints it, and published as that statement's format string, every value a `_` hole: "[FATAL] Got nullptr from CCDB for path Users/m/meide/… and timestamp 1500" becomes "Got nullptr from CCDB for path _ and timestamp _", with `fingerprint_src` a GitHub permalink to `CCDB/include/CCDB/BasicCCDBManager.h#L254` at the indexed commit. Public by construction: the text is the code's literal pieces only. Templates come from the format strings of `LOG*`, `throw …(…)`, O2's signposts, ROOT's `Error/Fatal/Warning(location, fmt…)` (printed with an optional `Class::` prefix) and any call whose name says it reports (`reportFatal`, …), from shell `echo`/`printf` and from Python `print`/`raise`/logging, in O2, O2Physics, O2DPG, QualityControl, ROOT, FairMQ and FairRoot (`SRC_REPOS`): ~55k templates, built in ~22 s, only when a head moved. Boost.Test messages count when the test file and case are found in those trees (the check expression too if it still is; otherwise it is a value), ctest failures when the test name is. A short fixed list covers the build tools' own messages (ninja, make, ld, the ctest summary, configure, rootcling, recc): kind `tool`, no permalink. C++ comments are blanked before the scan (commented-out code prints nothing), and a call-looking name followed by `{` after its arguments is a function's definition, not a call: on the indexed trees that drops about 3,300 junk sites, all of which a tree-sitter parse agrees are not calls, and changes no match on real failures (a full tree-sitter extractor was compared on 2026-11-17: one more signature matched out of 3,642, for 40 s instead of 33 s per build and a new dependency, so not adopted). `runner.sh` keeps the sources as shallow, sparse partial clones under `$COCO_ROOT/sources` (C++, shell, Python and CMake files only; ROOT's `interpreter/`, `roottest/`, `tutorials/` left out). The values (fillers) are normalised (numbers, pointers, hashes, dates, pids, ports, tmp paths) and HMAC'd with `kv/cocoindex` `filler_key` into `filler_hash`: two failures of one message with the same arguments share it, and a reader cannot confirm a guess at them. Without the key no filler hash is published, never a plain one. A compiler diagnostic's names (the `_` of its template: the header not found, the variable unused) are its fillers too, so `error: '_' file not found` splits by header without naming it; a crash fingerprint, entirely public, has none. So errors group three ways: `sig_hash` (the exact line), `template_id` (the message) and `filler_hash` (the message with the same values); `ci_signature` takes any of them. On this morning's snapshot (which predates crash fingerprints): of 3,642 signatures, 2,563 have a compiler template, 474 a source template, 134 a test template, 125 a tool template, 28 a CMake location, and 318 are hash-only (was 1,102). Of those 318, 192 are builder kinds whose `kind` says it all (disk-full, missing-system-dep, gpu-unavailable, …); the rest is a long tail: messages from code outside the sources or cut short, very long compiler errors, ACLiC command lines, whole o2-sim command lines (`fst-command`), which are best left hidden.
- **No residue.** `PRAGMA secure_delete` and a final VACUUM, so replaced and dropped text is not left in free pages.
- **Restore keeps the original.** The unsanitized DB travels in the state tarball (`raw/ci-failures.db`), which only the indexer reads; `restore()` prefers it, so a restored node gets the rows its state describes. Readers download only the sanitized DB. Note that the bucket's readers (the `ali-bot` keys) could still fetch the tarball: the boundary is what the MCP server serves, not what the bucket holds.
- An exception in the sanitizer fails the publish: nothing goes out unsanitized, and the previous snapshot stays current.

What it does not remove: CERN usernames in paths inside signatures (e.g. CCDB test paths `Users/<initial>/<login>/...`), and allocation ids inside alinomad.cern.ch links (pointers behind SSO, like `log_url`).

## Reader (`reader.py`)

1. GET `latest.json`.
2. For each db whose sha256 differs from the cached copy: download it, verify it, and `rename()` it into `~/.cache/cocoindex-reader/`.
3. Open it with `mode=ro&immutable=1` and load sqlite-vec. Vector search is brute-force `vec_distance_cosine`.
4. `connect(attach={"cf": "ci-failures.db", "pr": "prs.db"})` attaches the other flows. Unqualified names (`prs`, `pr_chunks`, `ci_builds`) resolve as well, so `commits c JOIN prs p ON p.repo = c.repo AND p.number = c.pr_number` and `prs p JOIN ci_builds b ON b.repo = p.repo AND b.pr = p.number` work.

The MCP server below is built on these functions. Proxy access works both on a Mac (the `security-proxy-token` CLI) and inside an allocation (the agent socket, spoken directly).

## MCP server (`mcp_server.py`, `cocoindex-mcp.nomad`)

How users reach the index. One program, two ways to run it:

| | central (default) | personal |
|---|---|---|
| transport | Streamable HTTP, stateless, JSON responses | stdio |
| runs on | Nomad job `cocoindex-mcp`, behind Traefik | the user's Mac, started by Claude Code |
| user needs | a CERN account with the ALICE role (SSO), or a bearer token for automation | read access to the bucket and an aigw route in their own proxy |
| set up with | `claude mcp add --transport http --client-id ali-ci-mcp --callback-port 33418 ali-ci https://ali-ci.cern.ch/mcp` (see "Auth" below) | `claude mcp add cocoindex -e COCO_STORE=s3://alice-cocoindex/cocoindex -- <venv>/bin/python mcp_server.py` |

**It serves snapshots only.** It never opens the indexer's live database. It reads the run that `latest.json` names, verifies each file against the manifest, and opens it `mode=ro&immutable=1`. So a reader never sees a half-finished pass, and stopping or resetting the indexer leaves the server running on the last good snapshot. A background thread checks `latest.json` every `SYNC_INTERVAL` (300 s); when nothing changed that costs one small GET. Tool calls never wait for a download: each worker thread reopens its connection when the run id changes, and `rename()` keeps the old file alive for queries still reading it. If a check fails, the server keeps serving the cached run and reports the error in `index_status`.

**It is a separate job from the indexer.** Its only state is a cache it can download again, so it can run anywhere and with `count > 1`. Stateless HTTP means Traefik needs no sticky sessions. A new version goes live only once `/healthz` answers, which it does only when a snapshot is loaded. Rollback is automatic (`auto_revert`); nothing in this job can corrupt data.

**Tools** (all marked read-only; the server's instructions tell the model when to use each):

| tool | what it does | calls aigw |
|---|---|---|
| `search_commits(query, repo?, since?, until?, k)` | cosine search over commit-message embeddings | 1 short embed (cached in an LRU) |
| `search_diffs(query, repo?, path_prefix?, since?, k, chunk_chars)` | cosine search over diff chunks; returns the matching slice and the commit subject | 1 |
| `find_commits(text?, path?, author?, repo?, pr?, since?, until?, k)` | exact filters (`LIKE` with wildcards escaped; path prefix through `commit_files`), newest first | no |
| `similar_commits(sha, repo?, k, same_repo)` | nearest commits to a given commit: follow-ups, reverts, backports | no |
| `get_commit(sha, repo?, diff, max_files)` | one commit in full; with `diff` also its indexed chunks (up to about 20k chars); its PR if the prs index has it | no |
| `search_prs(query, repo?, state?, author?, since?, until?, k)` | cosine search over PR descriptions, discussion chunks and unmerged diffs; one result per PR, with what matched best | 1 |
| `get_pr(number, repo?, discussion, max_chunks)` | one PR: metadata, description, merged commits (from commits.db), diff files, human discussion in time order (up to about 20k chars) | no |
| `search_ci_failures(query, repo?, check?, since?, k)` | cosine search over the distinct failure signatures; per hit: builds (failed/green), PRs, first/last seen, checks; the signature under `untrusted` | 1 |
| `ci_signature(sig, repo?, max_prs)` | one error, given as a pasted log line (normalised exactly as the indexer does) or a sig/class hash; `match` says exact, class, or nearest by meaning. It returns: first and last seen; builds (failed and green); distinct PRs, with a list; checks, hosts and per-week counts; `in_pr_files` yes/no; and `later_green_on_same_sha` (failed builds whose sha+check later passed: an observed later attempt, or GitHub's newest status) | only for nearest |
| `ci_pr_builds(repo, pr, errors_per_build)` | every build of a PR, newest first: sha, check, host, state and source, attempt, times, failed package, infra kinds, GitHub runs/failures, log URL. For failed builds, the top errors with `in_pr_files` and `other_prs_3d` (other PRs with the same signature within ±3 days) | no |
| `sql(query, max_rows)` | one read-only `SELECT`; the schema is in the tool description; `vec_distance_cosine` available | no |
| `index_status()` | run id, snapshot age (with a warning after 6 h), counts and newest commit per repo, diff-index cutoff; per-repo PR counts and whether the PR bootstrap is still running; per-repo CI builds, extracted and pending, builds by state, signatures embedded | no |

Repo arguments accept short names (`O2Physics`). An unknown repo is an error listing the indexed ones, not an empty result the model would read as "nothing found". `k` is capped at 50.

**Bounds on `sql`.** Four layers, so a model-written or hostile query can only read:
- the connection is `mode=ro&immutable=1`;
- an SQLite authorizer allows only SELECT, READ, FUNCTION and RECURSIVE, which refuses `ATTACH` of other files on the host, every `PRAGMA`, temp tables and `load_extension`;
- a progress handler stops the query after `SQL_TIMEOUT_S` (10 s);
- at most `MCP_MAX_SQL` (2) run at once, in a pool apart from the other tools' `MCP_MAX_CONCURRENT` slots, and a call waits at most `SQL_TIMEOUT_S` for its turn ("sql is busy" after that): four runaway queries used to hold every slot for 10 s, and `get_commit` waited behind them;
- results are capped at 500 rows, cells at 2000 chars, and vectors print as `<768-d vector>`.

The test runs eight hostile statements through it, including a runaway recursive CTE.

**Errors reach the model.** In mcp 2.x only a `ToolError` passes its text to the client; any other exception arrives as a bare "Error executing tool X". Caller mistakes therefore raise `BadRequest(ToolError)` with a message the model can act on ("unknown repo …; indexed: …", "query exceeded 10s; add a WHERE or LIMIT", "only SELECT …", "no snapshot yet, retry shortly").

**Auth (HTTP).** Each request except `/healthz` and the OAuth metadata needs `Authorization: Bearer <token>`, where the token is one of two kinds. Either kind admits a request, and the log line says which one did: `<user> POST /mcp via=sso` or `via=token`. Refusals are logged with the reason and, for a non-member, the user name. Tokens are never logged.

- **CERN SSO (people).** The token is an access token from `https://auth.cern.ch/auth/realms/cern`, obtained by the MCP client itself with PKCE. The server validates it locally:
  - the RS256 signature against the issuer's JWKS. `jwks_uri` comes from the discovery document and is checked to name the same issuer. The JWKS is cached and fetched again only when a token names an unknown `kid`, at most once per `OIDC_JWKS_MIN_REFRESH_S` (60 s);
  - `iss`, then `exp` and `nbf` with 30 s of leeway, then `aud` against `OIDC_AUDIENCE`. Keycloak's `typ` must be `Bearer`, so an ID token is refused;
  - `OIDC_REQUIRED_ROLE` in the `cern_roles` claim (`OIDC_ROLES_CLAIM`; a dotted path such as `resource_access.<client>.roles` also works).

  A missing, invalid or expired token gets **401** with `WWW-Authenticate: Bearer resource_metadata="https://ali-ci.cern.ch/.well-known/oauth-protected-resource/mcp"`. A valid token without the role gets **403** with "CERN SSO login OK for <user>, but the account lacks the 'alice-member' role". That 403 deliberately carries no `error="insufficient_scope"`: MCP clients treat that as a request to log in again with more scope, which would loop. The user is the first present of `cern_upn`, `preferred_username` and `sub`.
- **Static tokens (automation).** The server holds only **sha256 hashes**, rendered from Vault `kv/data/cocoindex-mcp` (`tokens` = JSON `{"<name>": "<hash>"}`) into `/secrets/mcp-tokens.json`, owned by the server's uid on every render. The server re-reads the file when its mtime changes, so adding or revoking a token needs no restart.

**Modes** follow from the configuration. `MCP_TOKENS_FILE` alone gives tokens only (the old behaviour, with a bare `WWW-Authenticate: Bearer`). `OIDC_ISSUER` alone gives SSO only. Both together accept either. With neither, the server refuses to start in HTTP mode, unless `MCP_ALLOW_ANONYMOUS=1` (tests). With `OIDC_ISSUER` it also refuses to start without `OIDC_AUDIENCE`, `OIDC_REQUIRED_ROLE` and `MCP_PUBLIC_URL`. The SDK's DNS-rebinding check rejects foreign `Host` headers with 421 (`MCP_ALLOWED_HOSTS`). Users never see the aigw key or the S3 keys: those stay in the job's proxy sidecar.

**OAuth discovery (RFC 9728).** The server is only a resource server: it issues nothing and stores no OAuth state. It serves protected-resource metadata without auth:

```json
{"resource": "https://ali-ci.cern.ch/mcp",
 "authorization_servers": ["https://auth.cern.ch/auth/realms/cern"],
 "scopes_supported": ["openid"], "bearer_methods_supported": ["header"]}
```

- `resource` is `MCP_PUBLIC_URL` verbatim. It comes from configuration, not from the request: behind Traefik the request says `/mcp` on a node port, and clients reject metadata whose `resource` is not the URL they dialled.
- The metadata is served at `/.well-known/oauth-protected-resource` + the public path (`.../mcp`), and at the bare `/.well-known/oauth-protected-resource` for an app mounted at the root.
- The metadata path is outside `/mcp`, so the jobspec has a second Traefik router for exactly that path.
- `scopes_supported` (`OIDC_SCOPES`) is what clients put in `scope=`. Without it, an SDK client falls back to all 26 scopes in CERN's discovery document. The role is deliberately not a scope.

**Why own middleware, not the SDK's `TokenVerifier`/`AuthSettings`.** mcp 2.3.0 can wire a token verifier, but it maps "valid but not allowed" onto scopes only. The role would then be advertised in the metadata's `scopes_supported`, so clients would request a scope CERN SSO does not know. A non-member would also get 403 `insufficient_scope`, which the SDK's own client (`client/auth/oauth2.py`) answers with a step-up re-login. `AuthSettings` also requires an issuer, so tokens-only mode could not be expressed. The existing `BearerAuth` ASGI middleware therefore grew an `OIDCVerifier` (pyjwt, already a dependency of mcp) and the metadata route. The test drives the SDK's real OAuth client through it.

**User setup.**
- **Claude Code:** `claude mcp add --transport http --client-id ali-ci-mcp --callback-port 33418 ali-ci https://ali-ci.cern.ch/mcp`. Then `/mcp` in a session (or `claude mcp login cocoindex`) opens the CERN login in the browser. `<id>` is the portal's client id and `<port>` the one registered in its `http://localhost:<port>/callback` redirect URI. `--client-id` is needed because CERN clients are registered in the Application Portal. CERN's discovery document does list a `registration_endpoint`, but anonymous dynamic registration is not expected to be allowed (not tested).
- **claude.ai and Claude Desktop:** Settings, Connectors, Add custom connector, URL `https://ali-ci.cern.ch/mcp`. Under Advanced, choose "Use your own OAuth client" and give the same client id. The redirect is `https://claude.ai/api/mcp/auth_callback`. These connectors call the server from Anthropic's cloud, not from the user's machine (see open decision 19).
- **Headless (CI, scripts, a remote shell):** either log in once where a browser is available (`claude mcp login`; the client can then refresh the token for as long as CERN's refresh token is valid), or use a static token: `--header "Authorization: Bearer <token>"`, minted as below.

**Application Portal (operator, once):** at application-portal.web.cern.ch:
1. Register the application `ali-ci-mcp` (owner: an ALICE admin e-group). One application per ALICE MCP: its client id is the tokens' `aud`, so a token for another ALICE MCP is refused here.
2. Under SSO registration, choose OIDC with a **public client** (no secret; PKCE S256). Use the redirect URIs `http://localhost:<port>/callback` (pick one fixed port for everyone) and `https://claude.ai/api/mcp/auth_callback`.
3. Under Roles, create the role `alice-member` and map it to the e-group `alice-member`. Optionally tick "role required to access", so that non-members are stopped at the login page rather than by the server's 403. The server checks the role either way.
4. Log in once with a test client and decode the access token. Read `aud` and `cern_roles` from it and set `OIDC_AUDIENCE` and `OIDC_REQUIRED_ROLE` in the jobspec accordingly. The audience must name this application: `cern_roles` holds the roles of the application the token is for, so a token for another CERN application that also defines an `alice-member` role is refused by `aud`, not by the role check.

The data itself is public: commit history of public GitHub repos. The token mainly protects the aigw quota (one embedding per search) and makes usage attributable. Per-user rate limiting is not implemented: `MCP_MAX_CONCURRENT` (4) bounds concurrent queries globally. A search's query embedding is computed before its query takes a slot, in a pool of its own (`MCP_MAX_EMBEDS`, 4, each bounded by `MCP_EMBED_TIMEOUT_S`, 20 s), so a slow embedder delays the searches only: with a 2 s embedder and 10 clients, `get_commit` went from 1.7 s to 2 ms at p50 (measured locally, 2026-11-17). Add it if one client turns out to dominate.

**Adding a static token** (automation; the human runs these; the token travels to its user once and is stored nowhere else):

```sh
tok=$(openssl rand -hex 32); echo "token for <name>: $tok"
hash=$(printf %s "$tok" | shasum -a 256 | cut -d' ' -f1)
vault-admin kv get -field=tokens kv/cocoindex-mcp \
  | jq -c --arg n "<name>" --arg h "$hash" '. + {($n): $h}' \
  | vault-admin kv patch kv/cocoindex-mcp tokens=-
```

To revoke, use `del(.["<name>"])` instead of `. + {…}`. The first time, start from `echo '{}'` instead of the `kv get`.

**Through LiteLLM?** aigw's MCP gateway can list this server, so users of aigw would reach it with their aigw key instead of a separate token. That needs the aigw admins to register the URL, and it adds a hop. Nothing in the server changes, so it can come later.

**Tests of SSO** (`test/sso_test.py <store-url>`, 89 checks on the prs store). `test/fake_oidc.py` is an in-process Keycloak-shaped issuer: discovery, a JWKS that counts fetches, RSA keys per run, a public PKCE client's `/auth` and `/token`, and token minting. The test covers:
- the three misconfigurations that must not start;
- the metadata at both paths, with `resource` equal to `MCP_PUBLIC_URL`, the issuer first and CORS;
- the 401 header;
- a member accepted; a non-member, and a token without the roles claim, refused with 403;
- 401 for each of: expired beyond the leeway, `nbf` in the future, wrong issuer, wrong audience, another key under the same `kid`, `alg=none`, HS256 keyed with the public key, an ID token, and garbage. A token expired 10 s ago is still accepted;
- one JWKS fetch through discovery and none for a known `kid`; a rotated key accepted after exactly one refetch; made-up `kid`s within the floor refused without a refetch;
- static tokens in "both" mode;
- `mcp_test.py`'s full `exercise()` (55 checks) through the real MCP client with an SSO token;
- a full login through the SDK's `OAuthClientProvider` (401, metadata, OIDC discovery, PKCE, token, tool call). Its authorize request carries the client id, S256, `resource` and `scope=openid`. A non-member's login ends in an error after one token, not in a loop;
- "SSO only" behind `https://ali-ci.cern.ch/mcp`: the metadata path Traefik forwards, the public URL in the 401, a static token refused, a foreign Host refused;
- user names and mechanism in the log, and none of the 19 tokens or their signatures in it.

**Tests** (`test/mcp_test.py <store-url>`; the URL must be an absolute `file://` URL): fake embedder plus the file:// snapshot from the end-to-end test, driven through the real MCP client, over stdio and over HTTP with a token file. 61 checks on a commits-only store; 114 on the store of `test/prs_test.py`, which adds 24 PR checks per transport; 188 on the store of `test/cif_test.py`, which adds 24 CI checks per transport (exact search hit, filters, a pasted log line matching exactly, another builder's copy of it, a class match, the GLUT error on 3 PRs, red-then-green counted, `ci_pr_builds` attempts and hosts, `other_prs_3d` = 2, secrets redacted, `sql` over `cf`, refused writes). The server now logs one plain line per record (`logging.basicConfig(force=True)`): the SDK's rich handler wrapped long lines at the terminal width, which split a refusal reason across two lines once this file grew, and `sso_test`'s log check failed on that alone.
- every tool, with ground truth taken from the cached db (with the fake embedder, the flow's exact text comes back at distance 0);
- LIKE escaping, the repo short-name mapping and the `k` cap;
- the eight refused `sql` statements, and the db intact afterwards;
- HTTP: 401 without a token or with a wrong one, 421 for a foreign Host, revocation by editing the file, caller name logged, token never logged;
- `/healthz` answers a Consul-style `Host: <ip>:<port>`.
- prs: the reader's indexes; exact-text hits on a description and a review-thread chunk; the `state`, `author` and `repo` filters; that no PR text appears outside `untrusted`; `get_pr`'s merged commits, caps and diff files; the PR on `get_commit`; the commits⋈prs join in `sql`; four more refused statements (`DETACH`, writes into `pr`, `pragma_database_list`).

**Tests of the prs flow** (`test/prs_test.py <workdir>`): fully offline. A local git origin, which the mirrors clone through `GIT_BASE_URL`, holds `refs/pull/<n>/head`. `test/fake_github.py` serves fixture PRs to the flow's five fixed queries, dispatched on `operationName`, with keyset cursors, nested pages, `nodes(ids:)` and injected rate limits. Seven runner passes with `FLOWS=commits,prs`, 55 checks:
1. The bootstrap is cut at 2 pages.
2. The next pass resumes from the saved cursor, through a 403, a `RATE_LIMITED` and a low `remaining`. Members, contributors and collaborators are indexed; the first-timer, the ghost and dependabot are not. Bot, linter, `alibuild` and minimized comments and HTML template comments are absent. Overflowing comments are paged in. The thread chunk carries path and line. The closed-unmerged and open PRs have diffs and the merged ones do not.
3. One comment is edited and one deleted, and the first-timer becomes CONTRIBUTOR without their PR being updated. The edit and the deletion are reflected, the PR is indexed, and exactly 3 texts are re-embedded.
4. Nothing changed: no embeddings, one list request, no snapshot.
5. A fresh host restores from the snapshot, re-embeds nothing, scans incrementally and ends with the same rows.
6. A new PR, and an embedder that does not answer (`fake_embedder.py` waits while `<log>.delay` exists): `PASS_TIMEOUT` (15 s) kills prs. The PR is not indexed, but `pr_repos` has the new count and watermark. Against the previous `app_main` this check fails with the old values, as in production.
7. The next pass, embedder back, indexes the PR.

## Metrics (`metrics.py`, Grafana dashboard `ci-jobs/grafana/ali-ci-mcp.json`)

Both jobs push OTLP gauges to MONIT, as `queue-metrics` and `bb-cache-metrics` do: through a `monit-otlp` proxy route (`https://monit-otlp.cern.ch:4319`, verified with the job's CERN CA bundle), whose credential `monit-otlp-auth` the bootstrap takes from `kv/data/cocoindex` `monit_otlp_auth`, else from `kv/data/ci` `queue_metrics_otlp_auth`. Without it the route answers 503 and nothing is sent; nothing else changes. A push that fails is logged once and dropped, never retried, never raised.

| from | metrics (prefix `cocoindex_`) | when |
|---|---|---|
| `runner.sh` | `flow_seconds`, `flow_timed_out`, `flow_exit_code` {flow, position}; `pass_seconds` | after each flow, each pass |
| the flows (litellm callbacks) | `embed_requests_started/ok/failed_count`, `embed_texts_count`/`_per_s`, `embed_request_seconds_p50/p95/max` {flow, route}; started − ok − failed is what cocoindex abandoned at `EMBED_TIMEOUT_S` | every minute while a flow runs |
| `ci_failures_flow.py` | `cif_new_builds`, `cif_logs_extracted`, `cif_logs_pending`, `cif_extract_mb(_per_s)`, `cif_github_requests/points`, `cif_github_budget_exhausted`, `cif_builds`, `cif_signatures` | each pass |
| `s3snap.py` | `snapshot_published_timestamp`, `snapshot_publish_seconds`, `snapshot_db_bytes` {flow}, `snapshot_signatures` {fingerprint: compiler, source, …, hash only} | each publish |
| `runner.sh` probe | `embed_probe_seconds`, `embed_probe_ok` {route} for each of `EMBED_PROBE_ROUTES` | every `PROBE_INTERVAL` (300 s) between passes |
| `mcp_server.py` | `mcp_calls_count` {tool, outcome: ok, bad_request, error}, `mcp_call_seconds_p50/p95/max` {tool}, `mcp_slot_wait_seconds_*`, `mcp_embed_seconds_*`, `mcp_embed_failed_count` {reason}, `mcp_sql_busy_count`, `mcp_snapshot_age_seconds`, `mcp_snapshot_ok`, `mcp_callers_1h`, `mcp_callers_24h` | every minute (`METRICS_INTERVAL_S`) |

Windowed values (`_count`, `_per_s`, percentiles) cover the minute before the push; a counter seen before reads 0 in a quiet minute rather than leaving a gap. Attributes are flows, routes, tools, outcomes and fingerprint kinds only: callers are counted in the server and never named, and no query, SQL or log text is ever an attribute. `mcp_test.py` and `cif_test.py` check exactly that against a fake receiver (`test/fake_otlp.py`); `metrics_test.py` covers the windows, failures and the probe.

Alerts worth setting in Grafana: `time() - max(last_over_time(cocoindex_snapshot_published_timestamp[2d])) > 6*3600` (the index is not being published), `max(cocoindex_mcp_snapshot_ok) == 0` for 30 min, and `sum(cocoindex_mcp_calls_count{outcome="error"}) > 0` for 15 min.

## Open decisions for you

The MCP server adds 8 to 11; the prs flow adds 12 to 16; SSO adds 17 to 20; the ci-failures flow adds 21 to 27.

1. **Bucket.** It is named `alice-cocoindex` as a placeholder. It needs creating on s3.cern.ch; the Mac's key currently sees only `TARS` and `alibuild-test`.
2. **Keys.** Either reuse `kv/data/s3-services`, the CI keypair; the route allowlist then provides the isolation, as it does for the CI jobs. Or create a dedicated pair in `kv/data/cocoindex`. Either way, `kv/data/cocoindex.aigw_key` must be created.
3. **Public-read bucket?** If it is public, readers need no credentials (`COCO_STORE=https://s3.cern.ch/<bucket>/cocoindex`). Otherwise the Mac's proxy needs that bucket in its `s3.buckets` map.
4. **Blobless vs. full mirrors.** Given the lazy-fetch cost above, full branch-only mirrors (`MIRROR_FILTER=`) are a one-line switch.
5. **Where the code lives.** It currently ships inside the jobspec via `file()`. A pinned clone (e.g. ali-bot/cocoindex at a commit) is the alternative.
6. **Image and pinning.** Decided 2026-11-16: both jobs run `registry.cern.ch/alisw/cocoindex@sha256:2643b976…` (docks run 43866765519), built from `requirements-linux.lock` at ali-bot 7c56be43; a lock change needs a new image and a digest bump in both jobspecs. The security-proxy sidecar is pinned to `security-proxy@sha256:39464dfb…` (amd64, 2026-10-27, ali-bot f7f4df6c), which has the per-bucket keypairs the `s3` route uses; the older `be422fab` that queue-metrics pins does not.
7. **Snapshot settings.** `SNAPSHOT_KEEP`=12 and `SNAPSHOT_MIN_INTERVAL`=0. Raising the interval trades less upload churn for more work lost on node loss.
8. **The MCP server's URL.** Decided 2026-11-15, changed 2026-11-17 from `/alice-mcp/mcp` (the host already says ali-ci; the portal application is `ali-ci-mcp`): `https://ali-ci.cern.ch/mcp`. Requests reach Apache on the `alibuild-frontend` nodes first, which terminates TLS and proxies to Traefik on `localhost:8090`; Traefik then routes on the Consul tags of `cocoindex-mcp.nomad`. Apache protects all of `ali-ci` with its own CERN SSO (mod_auth_openidc, client `ali-ci`) and answers a bearer token for another client with its own 401, before the MCP server is reached. So `it-puppet-hostgroup-alibuild`, `data/hostgroup/alibuild/frontend.yaml`, lists `/alice-mcp` and `/.well-known/oauth-protected-resource/alice-mcp` in the `ali-ci` service's `public_paths`, as it does for `/github`, and the MCP server does the authentication itself.
9. **Vault.** Create `kv/data/cocoindex-mcp` with `tokens='{}'`, and let the `nomad` role read it.
10. **Read-only S3 keys for the server.** It only reads the bucket. A read-only keypair (`kv/data/cocoindex-reader`) limits what a compromised server could do; until one exists it can share the indexer's.
11. **The `mcp` SDK.** It is pinned at 2.3.0 (2.x renamed FastMCP to MCPServer). Pin `mcp<2` instead only if a client needs it; the code would need porting back.
12. **GitHub PAT for the `github` route.** The jobspec reads `kv/data/cocoindex.github_token`, which needs creating. The repos are public, so a fine-grained token with no scopes is enough. The alternative is reusing `kv/data/ci`'s PAT, as queue-metrics does, but the bootstrap's ~5.5k points would then come out of the CI's hourly 5000. `PRS_RATE_RESERVE` only keeps a floor: 1500 since 2026-11-18, when GitHub reported 477-498 points left at the start of several prs passes, and once none, with the `alibuild` token in use.
13. **Edits that do not bump `updatedAt`.** The flow assumes that new comments and reviews bump the PR's `updatedAt`, which is the documented behaviour. It is not verified that *editing* or *deleting* a comment does. Such a change reaches the index the next time the PR is touched. If that is not enough, a periodic re-sweep (for example, re-fetch the PRs updated in the last 30 days once a day) is cheap: about 100 pages.
14. **Bot list.** `alibuild`, `github-actions`, `ALICE-CI-bot` and `dependabot`, plus `PRS_BOT_LOGINS`. PRs *opened* by these accounts are not indexed at all. Say so if alibuild-authored PRs (e.g. automatic bumps) should be.
15. **Untrusted commit text.** PR text is wrapped in `untrusted`. Commit subjects and bodies are equally third-party but are returned as before. Wrapping them changes the shape of every commit tool, so it is left for a separate change.
16. **Bootstrap order.** The repositories are scanned in order within one fetch budget, so AliceO2 finishes before O2Physics starts. Giving each repository its own share of the budget is easy if early coverage of all five matters more.
17. **E-group and role name.** Decided 2026-11-15: the e-group is `alice-member`. The portal role mapped to it is assumed to be called `alice-member` too (`OIDC_REQUIRED_ROLE`); change the jobspec if the role is named differently at registration. Still open: whether to tick "role required to access" in the portal.
18. **The `aud` value.** It is not known what CERN SSO puts in `aud` for this client's access tokens (the client id, a list, or `account` plus `azp`). `OIDC_AUDIENCE` is set to `cocoindex-mcp` as a placeholder and **must be verified with a real token at deployment**. If `aud` turns out not to name the client, add an audience mapper in the portal rather than turning the check off.
19. **Internet exposure.** Claude Code on a laptop calls the server from the user's machine, so CERN-internal reachability (or a tunnel) is enough for it. claude.ai and Claude Desktop connectors call it **from Anthropic's cloud**, so for those the URL (both the MCP path and the metadata path) must be reachable from the internet, which means a firewall opening for `ali-ci.cern.ch`. SSO with the role check is then the access control; `/healthz` and the metadata are the only unauthenticated paths, and they reveal the run id and the issuer.
20. **Scope and the `resource` parameter.** The metadata advertises `scope=openid`, and the SDK client adds `offline_access` when CERN lists it, which it does. Clients also send RFC 8707 `resource=https://ali-ci.cern.ch/mcp`, which Keycloak is assumed to ignore. Both are untested against the real CERN SSO; check them in the first real login. If the portal client needs another scope for `cern_roles` to appear in the access token, set `OIDC_SCOPES`.
21. **Read access to `alice-build-logs`.** The job's s3 route now allowlists `alice-build-logs` with the same keypair as the snapshots (`kv/data/s3-services`, the CI's own pair, which can write that bucket). The flow only GETs and lists, and `fake_s3.py` refuses anything else, but the proxy has no per-bucket read-only switch. Options:
    - a read-only keypair for `alice-build-logs` in `kv/data/cocoindex`, plus two more ingest slots;
    - a `"read_only": true` bucket option in security-proxy that refuses non-GET/HEAD;
    - accept the current setup.

    The MCP job does not need the bucket.
22. **The alidist commit is not in the log.** `pretty.html` only shows `ALIDIST_SLUG`, which is "Not pinned (most likely alidist@master)" on the CI. The minimal fix in `report-pr-errors`' `cat()` is one more header line, right after "Built commit": `print("Built with alidist", <alidist HEAD sha>, file=logf)`. The sha would come from `git -C alidist rev-parse HEAD` in the work dir, or from an env var `build-helpers.sh` already has. `logscan.parse_header` would then read it, and a broken alidist commit could be told apart from a broken PR across all PRs built on it. Not done here: ali-bot/ci is out of scope. **Decided 2026-11-16:** `report-pr-errors` now writes `Built with alidist <sha>` (from `git -C alidist rev-parse HEAD`) after "Built commit"; `logscan.parse_header` reads it into `ci_builds.alidist`. Logs from before the change keep NULL.
23. **Green logs are downloaded too** (after the failed ones): 56 of the 63 GiB. They give "seen in green builds" (noise detection) and complete per-build data. If that is not worth 4 extra bootstrap passes, a `CIF_GREEN_LOGS=0` switch would leave green builds with their `pretty.html` data only. **Decided 2026-11-16: keep them.**
24. **Status history cost.** REST, about 2.7 pages per commit, about 2.4k commits at bootstrap. That is about 6.5k REST calls out of the PAT's 5000/h core budget, spread over passes by `CIF_GH_BUDGET_S`. It shares the cocoindex PAT with the prs flow (GraphQL is a separate budget).
25. **Flakiness before the index existed.** For builds the flow never saw red, the only record is GitHub's status history (`gh_runs`, `gh_failures`), cut at 500 statuses (5 pages) per commit. Very busy commits lose their oldest runs.
26. **What counts as "not this PR's fault".** The tools report facts: `in_pr_files`, infra kinds, `other_prs_3d`, green builds with the same signature, and later-green share. The ±3-day window and "several PRs" are presentation choices, and no threshold is applied. Calibrate on real use before adding a verdict field.
27. **Cleaning up the check name.** Contexts are learnt from the status histories. Until one history mentions a context, its name is the `_` → `/` guess (wrong for e.g. `build/O2/fullCI_alidist_slc9`). `check_dir` is always exact.
