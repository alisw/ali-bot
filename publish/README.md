aliPublish
==========

Publish software built using aliBuild on a variety of output formats by taking
care of dependencies.


Introduction
------------

[aliBuild](http://alisw.github.io/alibuild/) has an option to rsync locally
built packages on a remote repository (`--remote-store`). Packages will be
written remotely as tarballs with a certain directory structure and symbolic
links that define their dependencies.

aliPublish is independent from aliBuild and understand such directory structure
in order to perform consistent packages installation. When installed, packages
are relocated correctly. aliBuild will not be needed for running them.

The public software repository where tarballs are stored is a static directory
exported via the nginx server with [JSON directory listing](http://nginx.org/en/docs/http/ngx_http_autoindex_module.html) enabled.


Output destinations
-------------------

Currently aliPublish is capable of outputting the packages in the following
formats.

  - **CVMFS.** Unpacks packages on a CernVM-FS Stratum-0 and publishes them
    using standard CVMFS commands. Used in production on `alice.cern.ch` and
    `alice-nightlies.cern.ch`.
  - **AliEn PackMan.** Uploads packages on the ALICE AliEn PackMan legacy
    system. This is essential for us to have the packages known by
    [AliEn](http://alimonitor.cern.ch/packages/).
  - **Directory.** Just unpacks packages under a certain directory structures.
    Used in production on
    [Titan at Oak Ridge](https://www.olcf.ornl.gov/titan/) where CVMFS is not
    available.
  - **Yum repository.** Generates consistent RPMs out of the tarballs and
    creates a Yum repository with `createrepo`.


Configuration
-------------

aliPublish expects by default to have a file called `aliPublish.conf` in the
current directory.

You can base your configuration upon [this example](aliPublish-example.conf).

Example configuration:

```yaml
base_url: https://ali-ci.cern.ch/TARS
architectures:
  slc5_x86-64:                   # aliBuild input architecture
    CVMFS: x86_64-2.6-gnu-4.1.2  # how is this arch called on CVMFS
    AliEn: Linux-x86_64          # how is this arch called on AliEn
    RPM: el5.x86_64              # how is this arch called on RPMs
    dir: el5.x86_64              # how is this arch called on the directory output
    include:
      AliDPG:
       - ^v5-0[678]-XX-(Titan-)?[0-9]+$
      AliPhysics:
       - ^vAN-201[0-9][0-1][0-9][0-3][0-9]-[0-9]+$           # regular expressions to include packages
       - ^v5-0[678]-[0-9]+[a-z]?-(p[0-9]+-)?0[1-9]-[0-9]+$
      AliGenerators: true                                    # all versions for this package are installed
    exclude:
      AliPhysics:
       - ^vAN-20150910.*$
      EPOS:
       - v3\.111-[1-7]$
  ubt1604_x86-64:
    CVMFS: ubuntu1604-x86_64
    dir: ubuntu1604-x86_64
    AliEn: false               # ignore this arch on AliEn
    RPM: false                 # ignore this arch on RPMs
    include:
      GCC-Toolchain: ^v4\.9\.3-[0-9]+$

# CVMFS-specific configuration
cvmfs_repository: alice.cern.ch
cvmfs_package_dir: /cvmfs/%(repo)s/%(arch)s/Packages/%(package)s/%(version)s
cvmfs_modulefile: /cvmfs/%(repo)s/%(arch)s/Modules/modulefiles/%(package)s/%(version)s

# Directory output specific configuration
package_dir: /opt/mysoftware/%(arch)s/Packages/%(package)s/%(version)s
modulefile: /opt/mysoftware/%(arch)s/Modules/modulefiles/%(package)s/%(version)s

# RPM-specific configuration
rpm_repo_dir: /repo/RPMS

# Send email notifications (optional)
notification_email:
  server: cernmx.cern.ch
  package_format: "  - %(package)s %(version)s\n"
  success:
    body: |
      Dear all, package %(package)s %(version)s was installed. Dependencies:

      %(alldependencies_fmt)s
    subject: "[publisher] %(package)s %(version)s published"
    from: "publisher <publisher@instutute.gov>"
    to:
      AliRoot: all-collaboration@insitute.gov      # send notification for this package only
      AliPhysics: all-collaboration@insitute.gov
      default: admins@institute.gov                # send notification for unspecified packages
  failure:
    body: |
      Publishing failed for %(package)s %(version)s. Please have a look.
    subject: "[publisher] %(package)s %(version)s failed"
    from: "publisher <publisher@instutute.gov>"
    to: admins@institute.gov

# What packages to publish
auto_include_deps: True        # automatically include all dependencies
filter_order: include,exclude  # include directives are processed before exclude

# Avoid connection flooding and retry
conn_retries: 10
conn_dethrottle_s: 0.07
conn_timeout_s: 6.05

# Optionally turn off SSL certificate verification (dangerous)
http_ssl_verify: False
```


Run aliPublish
--------------

Instant gratification (assuming `aliPublish.conf` is in your current directory):

```
./aliPublish sync-dir --dry-run --debug
```

You can remove `--dry-run` to actually download and unpack packages, and you can
remove the `--debug` to make the output less verbose.

`sync-dir` is the simplest option that unpacks the defined packages (and their
dependencies) into the specified prefix. Other run modes are `sync-cvmfs`,
`sync-rpms` and `sync-alien`.

On production servers you will have an instance of aliPublish running
automatically every once in a while (_e.g._ in a cron job). In this case use the
option `--pidfile` to prevent multiple instances from running in parallel. It is
recommended to automatically update aliPublish from the Git repository before
each run.


Installation on production servers
----------------------------------

For the ALICE supported use cases we keep all configurations in this repository
under different names:

  - `aliPublish.conf`: used centrally on CVMFS and AliEn
  - `aliPublish-titan.conf`: packages published on Titan
  - `aliPublish-nightlies.conf`: configuration for publishing test releases

We then have a `get-and-run.sh` script that automatically performs the
repository update (which contains both aliPublish and its configuration). Your
use case has to be supported by the script.

To install aliPublish on a production server, do only once:

```bash
mkdir publisher
cd publisher
curl -LO https://raw.githubusercontent.com/alisw/ali-bot/master/publish/get-and-run.sh
chmod +x get-and-run.sh
```

The script is ready to be put in a crontab like this for instance:

```
*/20 * * * *   /full/path/to/get-and-run.sh > /dev/null 2>&1
```

> It is recommended to always run aliPublish in a cron job. This way package
> installation can be completely unmanned. aliPublish is made in a way that
> temporary failures in a package installation will be automatically fixed in
> the next run.


How to publish new packages
---------------------------

Assuming your use case is centrally supported by ALICE and you are running it
through the `get-and-run.sh` script then the best way to publish new packages is
to open a pull request to this repository. Once the pull request is merged the
next aliPublish run will pick up the new configuration and apply it
automatically.


How to unpublish existing packages
----------------------------------

The utility `aliUnpublish` is used to clean up published packages available on
CVMFS and AliEn. You need to run it on a machine with CVMFS enabled, and as a
result it will produce two shell scripts, one to be run on the CVMFS publisher
node, one to be run on the AliEn publisher node.

You can run:

    aliUnpublish

without any parameter. As said, this will create two scripts for the actual
cleanup but it will not delete anything. It applies the default policy:

  - Only `vAN-` packages are considered, using `AliPhysics` as package name.
  - Packages older than 60 days will be condemned, except the first package for
    each month (forever kept).
  - Packages on CVMFS will be archived and not deleted.

A testfile to pass to `aliPublish test-rules` will also be created (instructions
will be printed) to test if your `aliPublish.conf` contains the correct rules:
unpublished packages should be excluded in that configuration file for
preventing them from reappearing unwantedly.

To see more options, run:

    aliUnpublish --help
