Pull requests processor
=======================

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
