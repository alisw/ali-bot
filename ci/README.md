Pull requests processor
=======================

This folder contains the helper scripts which run our continuous integration.

The core of the testing is `claim-builder.sh`, which each round asks
`list-branch-pr` what is queued for the containers it serves, takes a
`nomad var lock` on (check, commit) so exactly one worker builds it, and runs
`build-one.sh` -> `build-loop.sh` to merge the pull request into a local
checkout and invoke `aliBuild`.

It replaced `continuous-builder.sh`, which sharded the queue by worker index
instead of claiming it, re-exec'd itself between iterations, and served one
container per builder. That script was removed on 2026-09-30, once the last
fleet running it had been retired; its inner loop survives as `build-loop.sh`,
which the claim path still uses unchanged.

The tradeoffs that remain:

* A worker serves the containers named in `CUR_CONTAINERS`, not just one.
* We keep testing broken pull requests, although with less frequency compared to
  newly introduced ones, which get precedence.

This allows us to be more resistant to transient errors, since we keep retesting until
something is merged.

Parallelisation happens by partitioning the git hashes space among sever workers in a predefined manner.
This could introduce some latency and inefficiencies in the case there are two pull requests
which end up in the same partition, but it avoids having to maintain a central scheduler for our jobs.

Tools details
=============


queue-metrics.sh
----------------

Reports how much work is queued for one pool of builders, without building
anything. One invocation covers one `MESOS_ROLE`/`CUR_CONTAINER` pair; it is
deployed as `queue-metrics.nomad` in the [ci-jobs][] repository, a *single*
allocation that discovers the pools from `repo-config/` and runs one round per
pool, so the GitHub API cost does not grow with the number of pools or
builders. It need not run on the machines it reports about.

It exists because the builders cannot measure this themselves. Each one sees
only its own shard of the pull requests (the hash partitioning described above),
and when that shard is empty it falls back to rebuilding an already-tested PR
rather than idling — so a busy builder tells us nothing about whether real work
is waiting.

Parameters (as environment variables):

* `GITHUB_TOKEN`, `MESOS_ROLE`: as for the builders.
* `OTLP_METRICS_URL`: where to POST metrics, e.g.
  `https://monit-otlp.cern.ch:4319/v1/metrics`. Unset means "do not report",
  which is what makes the script safe to run by hand.
* `OTLP_WRITE_TOKEN`: sent as `Authorization: Bearer`. Through the
  security-proxy this is a rotating gate token, not the real credential.
* `CUR_CONTAINER`: short container name, e.g. `slc9`. Derived from
  `CONTAINER_IMAGE` if unset, by the same derivation the builders use, so the
  job can be given the same variables as the pool it watches.
* `QUEUE_METRICS_INTERVAL`: seconds between polls (default 300). Can be
  overridden at runtime through `config/queue-metrics-interval`.

Pass `--once` to report a single round and exit rather than looping. The Nomad
job uses this because it holds a credential that expires -- a gate token from
the security-proxy -- and must re-resolve it every round: the script's own
re-exec would inherit the environment and keep using a stale one.

[ci-jobs]: https://github.com/alisw/ci-jobs

Metrics go to [Mimir][], MONIT's Prometheus-compatible store, over OTLP —
not to InfluxDB, which DBOD stops and deletes on 2027-01-01. `otlp-push.py`
does the translation: an InfluxDB point carries several named fields, a
Prometheus series carries one value, so each field becomes its own gauge.

* `ci_queue_untested`, `ci_queue_failed`, `ci_queue_succeeded` and
  `ci_queue_oldest_untested_wait_seconds`, labelled with `role`, `container`,
  `checkname` and `repo`. Checks whose queue is empty report zero, so that "no
  work" and "no data" can be told apart.
* `ci_queue_poll_ok`, recording whether GitHub could be reached. When it could
  not, no `ci_queue_*` samples are written at all — an outage must not look
  like an empty queue to anything scaling off these numbers.

There is deliberately no `total` (it is the sum of the other three, and a
`_total` suffix means a counter in Prometheus) and no `host` label (it
describes the collector, which moves between nodes, and would start a fresh
series for every check on every reschedule).

[Mimir]: https://monit.docs.cern.ch/metrics/otlp/

The collector is strictly read-only with respect to GitHub: it passes
`--no-status` to `list-branch-pr`, so it cannot interfere with the statuses set
by the builders.


process-pull-requests
---------------------

`process-pull-requests` processes all open and mergeable pull requests from the
configured repositories in `perms.yml`. Configuration files:

* `perms.yml`: sets rules via regexps and permissions, for all the repositories,
  and defines internal groups
* `groups.yml`: external groups (for instance CERN egroups): they are overridden
  by internal groups with the same name
* `mapusers.yml`: mapping between usernames as specified in the first two files
  and GitHub users; for instance, maps CERN accounts with GitHub


sync-egroups.py
---------------

This utility gets all CERN e-groups defined in the current `perms.yml` and
queries the CERN LDAP for finding all members, recursively. Groups are meant to
be stored to `groups.yml`:

    ./sync-egroups.py > groups.yml


runner.sh
---------
Periodically runs the sync of egroups, pushes changes (if any), and the pull
requests processor. Automatically updates from a given repository/branch.

Parameters (as environment variables):

* `GITLAB_TOKEN`: CERN GitLab token associated to the service account user, used
  to pull/push configuration from the private GitLab repository.
* `CI_ADMINS`: comma-separated list of GitHub users acting as administrators.
* `CI_REPO`: GitHub `user/repo[:branch]` containing the scripts.
* `SLEEP`: seconds to sleep after runs.
* `PR_TOKEN`: GitHub token used to communicate with the GitHub API.



* `--list`: list PRs to process and exit. Useful to test the GitHub API
* `--test-doctor`: run aliDoctor and exit. Useful to test system dependencies
* `--test-build`: run aliBuild once without testing any PR and exit. Useful to warm up the CI

Normal, non-interactive operations require no option.
