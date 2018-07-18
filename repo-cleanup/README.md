Cleaning up aliBuild's tarballs repository
==========================================

aliBuild tarballs repository grows over time. Since it is used as a base for publishing packages,
and since some of the packages there should not be kept forever, a cleanup tool meant to run
automatically exists.

The cleanup tool, found in this directory, is called `repo-cleanup.py`. It is a Python script with
no options and a single YAML configuration file, `repo-cleanup.yml`.


Usage
-----

Once the rules are defined (see [Configuration](#configuration)) it is as simple as running the
script on the node containing the repository:

```bash
./repo-cleanup.py
```

It is convenient to have it running automatically as a cronjob, for instance once per week:

```bash
42 4 * * 6  /opt/ali-bot/repo-cleanup/repo-cleanup.py > /tmp/repo-cleanup.log 2>&1
```

This will run the script every week at 4.42am on Saturdays.


Configuration
-------------

This is an example configuration section:

```yaml
# O2: remove all nightly builds of all flavours (e.g. release, _TEST, etc.)
# older than 30 days. Do not keep any old build
O2:
  - ^nightly-[0-9]{8}-:
      purgeOlderThan: 30

# AliPhysics: various cleanup policies
AliPhysics:
  # Release candidates must go in 60 days
  - -rc[0-9]+:
      purgeOlderThan: 60
  # Until 20160430 (included): keep *last* tag of the month
  - ^vAN-201(5[0-9]{2}|60[1234])[0-9]{2}-[0-9]+$:
      purgeOlderThan: 90
      excludeFromPurge: lastOfMonth
  # Later: keep *first* tag of the month
  - ^vAN-[0-9]{8}-[0-9]+$:
      purgeOlderThan: 90
      excludeFromPurge: firstOfMonth
  # Keep forever all normal tags in the form: v5-09-33(a)(-p1)-01(-bahbah)-1
  - ^v[0-9]-[0-9]+-[0-9]+[a-z]*(-p[0-9]+)?-[0-9]+(-[a-z0-9]+)?-[0-9]+:
      purgeOlderThan: never
  # What is the rest? Unknown, remove in half a year
  - .*:
      purgeOlderThan: 180
```

From the example above we notice that we define _toplevel_ packages (`O2` and `AliPhysics` in our
case), and a list of rules for them. The `O2` package, notably, has a single rule: the rule is
defined by a regular expression that matches its **version number, including the revision**, and
an action that tells what to do.

In the `O2` case, all "nightly" builds older than 30 days will be removed.

The `AliPhysics` example is more elaborate. There is a list of rules: the cleanup script will stop
at the first one matching. We can see that we can also specify `never` as value of `purgeOlderThan`,
to clearly tell the cleanup script **not to clean that package**.

It is also possible to exclude from cleanup certain matching packages. In particular, we can see
that `AliPhysics` versions whose name starts with `vAN-<date>` are cleaned up if older than 90 days,
but the exclude rule will always keep the first valid version for each month (`firstOfMonth`).
Similarly, it's possible to keep the last valid version (`lastOfMonth`). This only works if there is
a 8-digit number resembling a date in the version name.

There is a single configuration option telling the cleanup script where the tarballs repository is:

```yaml
repo_cleanup_configuration:
  tarballs_prefix: /build/reports/repo/TARS
```
