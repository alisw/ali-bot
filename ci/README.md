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


convert-from-gitolite.py
------------------------

Utility to convert permissions from legacy Gitolite format to the format
expected by `perms.yml`. This is not a generic conversion tool but it
specifically aims the special format conventions used in ALICE.

    ./convert-from-gitolite.py gitoliteRepo1.conf:alisw/Repo1 \
                               gitoliteRepo2.conf:alisw/Repo2 \
                               ...

All input files (_e.g._ `gitoliteRepo1.conf`) are mapped to a certain GitHub
repository (_e.g._ `alisw/Repo1`, in the format `user/repo`).


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
