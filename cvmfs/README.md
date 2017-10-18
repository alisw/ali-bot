Populate a CVMFS tree
=====================

One might want to populate a CVMFS-like tree on a separate prefix for a number
of reasons, for instance a supercomputer with a shared namespace where `/cvmfs`
is not available. Or, you can have a separate CVMFS Stratum-0 managed on your
own.

This is done by running [aliPublish](../publisher) in directory mode
(`sync-dir`) or CVMFS mode (`sync-cvmfs`).

There is however a list of files that must be installed and updated manually
for ALICE operations to work:

  - the [alienv](alienv) script setting the environment
  - the correct toolchain modulefile pointing to a compiler/linker version
    compatible with the running platform


Install alienv
--------------

alienv has to be mandatorily installed under `$PREFIX/bin` and it must be a
regular file (not a symbolic link for instance).

> The authoritative one (which passes automated tests before being published) is
> from the master branch of the current repository.

You can fetch it by doing:

```sh
cd $PREFIX
mkdir bin
curl -Lo bin/alienv https://raw.githubusercontent.com/alisw/ali-bot/master/cvmfs/alienv
chmod 0755 bin/alienv
```


Install toolchain
-----------------

Currently by default ALICE is building all its software against a GCC-Toolchain
package on a SLC5 platform. The GCC-Toolchain package for SLC5 is automatically
installed as a dependency by aliPublish.

Packages built for SLC5 are successfully being run on different platforms
provided that the appropriate GCC-Toolchain which is platform-specific is loaded
in place of the SLC5 one. alienv is configured to fall back on the default
GCC-Toolchain for SLC5 in case no appropriate toolchain for the current platform
is available.

Before installing the toolchain you must find out if your current platform is
recognized by alienv. You can test it by simply running:

```sh
ALIENV_DEBUG=1 $PREFIX/bin/alienv
```

and see if some "unknown platform" error pops up. The detected platform goes
under the line `platform=`.

> If the current platform is unknown then you should edit the alienv script and
> open a pull request, or just contact the admins.

If the current platform is known, chances are ALICE provides already a
GCC-Toolchain binary package for that platform too. You should then edit the
`aliPublish.conf` file in order to include this package, for instance (the
example is for Ubuntu 16):

```yaml
architecture:
  ubt1604_x86-64:
    dir: ubuntu1604-x86_64
    include:
      GCC-Toolchain: ^v4\.9\.3-1$
```

This way the package will be installed by aliPublish at its next run. Note that
syntaxes like:

```yaml
latest_gcc_conf: &latest_gcc
  - ^v4\.9\.3-alice3-1$

architecture:
  ubt1604_x86-64:
    dir: ubuntu1604-x86_64
    include:
      GCC-Toolchain: *latest_gcc
```

are YAML variable substitutions.

In most cases (_e.g._ supercomputers) a decent GCC version (we use v4.9.3) is
already provided with a [modulefile](http://modules.sourceforge.net/) that can
be loaded.

What you need to do is to create a symlink from the modulefile loading the
toolchain you wish to use to the place where alienv expects to find it for the
current platform (`ubuntu1604-x86_64` in the example below):

```
ln -nfs $SOURCE_MODULEFILE $PREFIX/etc/toolchain/modulefiles/ubuntu1604-x86_64/Toolchain/GCC-v4.9.3
```

Now `$SOURCE_MODULEFILE` will be either a custom path, or something like:

```
$PREFIX/ubuntu1604-x86_64/Modules/modulefiles/GCC-Toolchain/v4.9.3-alice3-1
```

in case you have installed it with aliPublish.


alienv architectures
--------------------

The `alienv` script used on the Grid for CVMFS contained in this directory
currently supports the following way of selecting software architectures.

When running:

    alienv [enter|load|printenv] Package1/Ver1 Package2/Ver2...

`alienv` looks for the given packages under a list of preferred architectures,
defined by the variable `PLATFORM_PRIORITY`. The first architecture where all
of the given packages are found is selected. Packages from mixed architectures
cannot be loaded.

`alienv` will anyways try to load the "runtime environment" (C++ compiler and
libraries mostly) for the _currently detected architecture_, even if the rest
of the packages are taken from another one.

When a new platform is added to the system, the variable `PLATFORM_PRIORITY` has
to be changed accordingly, and the new compiler toolchain must be added to the
`etc/toolchain/modulefiles` directory as described in the previous paragraph.
