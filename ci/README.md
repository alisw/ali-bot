Pull requests processor
=======================

This folder contains the helper scripts which run our continuous integration.

The core of the testing is the `continuous-builder.sh` script which loops on
the open pull requests of a given repository and invokes `aliBuild` of a given
package after merging the contents of a given pull request to a local checkout
of the given repository. This means that we have the following tradeoffs:

* We can test PRs for a single repository per builder.
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
anything. Run it as a single service per `MESOS_ROLE`/`CUR_CONTAINER` pair,
alongside the builders of that pool.

It exists because the builders cannot measure this themselves. Each one sees
only its own shard of the pull requests (the hash partitioning described above),
and when that shard is empty it falls back to rebuilding an already-tested PR
rather than idling — so a busy builder tells us nothing about whether real work
is waiting.

Parameters (as environment variables):

* `GITHUB_TOKEN`, `INFLUXDB_WRITE_URL`, `MESOS_ROLE`: as for
  `continuous-builder.sh`.
* `CUR_CONTAINER`: short container name, e.g. `slc9`. Derived from
  `CONTAINER_IMAGE` if unset, exactly as `continuous-builder.sh` does it, so the
  job can be given the same variables as the pool it watches.
* `QUEUE_METRICS_INTERVAL`: seconds between polls (default 300). Can be
  overridden at runtime through `config/queue-metrics-interval`.

Two InfluxDB measurements are written:

* `ci_queue`, one point per check, tagged with `checkname` and `repo`, with
  fields `untested`, `failed`, `succeeded`, `total` and
  `oldest_untested_wait_secs`. Checks whose queue is empty report zeroes, so
  that "no work" and "no data" can be told apart.
* `ci_queue_poll`, with a single `ok` field, recording whether GitHub could be
  reached. When it could not, no `ci_queue` points are written at all — an
  outage must not look like an empty queue to anything scaling off these
  numbers.

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


run-continuous-builder.sh
-------------------------
This script is used to run the continuous builder without Aurora. This is useful for running it on
macOS, for instance.

Usage:

```bash
./run-continuous-builder.sh <profile> [--test-build] [--test-doctor] [--list]
```

`<profile>` refers to `<path_to_this_script>/conf/<profile>.sh`, containing a configuration in the
form of shell variables (the script will be sourced).

* `--list`: list PRs to process and exit. Useful to test the GitHub API
* `--test-doctor`: run aliDoctor and exit. Useful to test system dependencies
* `--test-build`: run aliBuild once without testing any PR and exit. Useful to warm up the CI

Normal, non-interactive operations require no option.
