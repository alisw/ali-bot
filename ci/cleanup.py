#!/usr/bin/env python3

"""Clean up aliBuild build area for CI workers.

In addition to what `aliBuild clean` does, this can also delete old builds, and
is a bit more aggressive in order to keep disk space from growing indefinitely.
"""

import logging
from argparse import ArgumentParser, FileType
from collections import defaultdict
from itertools import chain, groupby
from operator import itemgetter
from pathlib import Path
from shutil import disk_usage
from string import Template
from time import time, monotonic
import typing

from alibuild_helpers.clean import doClean
from alibot_helpers.utilities import parse_env_file

if typing.TYPE_CHECKING:
    from argparse import Namespace
    from collections.abc import Iterable
    from io import StringIO
    SymlinkWithInfo: typing.TypeAlias = tuple[float, Path, 'CIEnvironment']


LOGGER = logging.getLogger(__name__)


def main(args: 'Namespace') -> 'None':
    """Script entry point."""
    initial_free = disk_usage(args.ci_root).free
    builder_defs_dir = \
        args.definitions_dir / args.mesos_role / args.container_name
    ci_envs = [CIEnvironment(envf, args.ci_root, args.work_dir, initial_free)
               for envf in builder_defs_dir.glob("*.env")
               if envf.name != "DEFAULTS.env"]
    # Sort symlinks newest-first, so we can efficiently pop() the newest ones
    # off the end later.
    symlinks: 'list[SymlinkWithInfo]' = \
        sorted(chain(*(ci_env.gather_symlinks() for ci_env in ci_envs)),
               key=itemgetter(0), reverse=True)
    LOGGER.debug("found %d symlinks in %d environments to consider",
                 len(symlinks), len(ci_envs))

    if args.maximum_age is not None:
        symlinks = delete_old_builds(symlinks, args.maximum_age,
                                     dry_run=args.dry_run)

    # First cleanup pass: delete temporary sources, tarballs, and any old
    # builds/installations (whose symlinks were deleted above).
    for ci_env in ci_envs:
        ci_env.cleanup(dry_run=args.dry_run)

    # Delete old builds until enough disk space is free.
    if args.minimum_disk_space is not None:
        cleanup_to_disk_threshold(symlinks, args.ci_root,
                                  args.minimum_disk_space,
                                  dry_run=args.dry_run)

    # We're done cleaning up. Just print metrics, if requested.
    for ci_env in ci_envs:
        ci_env.print_metrics(args.metrics_file)


class CIEnvironment:
    """A CI environment specifying a main repo and dev packages.

    A CI environment is declared by a .env file, and has a directory on disk
    named after itself, inside which its aliBuild work directory is stored.
    """
    name: 'str'
    """The name of the .env file (e.g. 'o2-alidist') for this environment."""
    packages: 'frozenset[str]'
    """Names of development packages to clean up in this environment."""
    num_deleted_symlinks: 'int' = 0
    """Total number of symlinks deleted in this environment."""
    cleanup_duration: 'float' = 0.0
    """Total time spent (in seconds) cleaning up inside this environment."""
    freed_bytes: 'int' = 0
    """Approximate total number of bytes freed inside this environment."""
    initial_disk_free_bytes: 'int' = 0
    """Number of bytes free on disk before any cleanup in any environment."""

    def __init__(self: 'CIEnvironment', env_file_path: 'Path',
                 root_dir: 'Path', work_dir_name: 'str',
                 initial_disk_free_bytes: 'int') -> 'None':
        self.name = env_file_path.name[:-len(".env")] \
            if env_file_path.name.endswith(".env") else env_file_path.name
        self.work_dir = root_dir / self.name / work_dir_name
        self.initial_disk_free_bytes = initial_disk_free_bytes
        # Mirror the behaviour of shell variables -- empty string if unset.
        env_vars = defaultdict(str, parse_env_file(env_file_path))
        # string.Template uses $var/${var} syntax, like the shell.
        devel_pkgs = Template(env_vars["DEVEL_PKGS"])
        self.packages = parse_devel_pkg_spec(devel_pkgs.substitute(env_vars))
        LOGGER.debug("%s: development packges are %s",
                     env_file_path.name, ", ".join(self.packages))

    def __repr__(self: 'CIEnvironment') -> 'str':
        return (f"CIEnvironment({self.name}, exists={self.work_dir.exists()}, "
                f"packages={list(self.packages)!r})")

    def gather_symlinks(self: 'CIEnvironment') -> 'Iterable[SymlinkWithInfo]':
        """Find candidates for cleanup among build and installation symlinks.

        Walk the relevant directories in the CI build directory, considering
        development packages only.

        Generate tuples describing symlinks which can be cleaned up, consisting
        of the symlink's own modification time, the path to the symlink and the
        environment it was found inside (to make later cleanup faster).
        """
        return (
            (entry.lstat().st_mtime, entry, self)
            for entry in chain(self.work_dir.glob("BUILD/*"), (
                    entry
                    for pkg_dir in self.work_dir.glob("*_*/*")
                    # Path.glob("*/") doesn't only select directories,
                    # so explicitly filter for directories.
                    if pkg_dir.is_dir() and pkg_dir.name in self.packages
                    for entry in pkg_dir.iterdir()
            ))
            if entry.is_symlink()
        )

    def delete_symlink(self: 'CIEnvironment', symlink: 'Path', reason: 'str',
                       *, dry_run: 'bool') -> 'None':
        """Delete the given symlink, updating statistics.

        This method ensures that the symlink's target is inside this
        environment's working directory. If not, a ValueError is raised.
        """
        # Path.is_relative_to() would be better, but that's Python 3.9+ only.
        if self.work_dir.resolve() not in symlink.resolve().parents:
            raise ValueError("symlink not in this environment", self,
                             self.work_dir.resolve(), symlink.resolve())
        if dry_run:
            LOGGER.info("would delete %s (%s)", symlink, reason)
        else:
            LOGGER.info("deleting %s (%s)", symlink, reason)
            symlink.unlink()
        self.num_deleted_symlinks += 1

    def cleanup(self: 'CIEnvironment', *, dry_run: 'bool') -> 'None':
        """Clean up inside this environment.

        Do the same thing as `aliBuild clean` for every architecture we find.
        """
        if not self.work_dir.exists():
            return
        cleanup_start = monotonic()
        free_before_start = disk_usage(self.work_dir).free
        for arch_dir in self.work_dir.glob("*_*"):
            LOGGER.info("cleaning up %s", arch_dir)
            try:
                doClean(str(arch_dir.parent), arch_dir.name,
                        aggressiveCleanup=True, dryRun=dry_run)
            except SystemExit as exc:
                # Annoyingly, doClean calls sys.exit.
                if exc.code != 0:
                    LOGGER.error("doClean exited with error %d", exc.code)
        self.cleanup_duration += monotonic() - cleanup_start
        self.freed_bytes += disk_usage(self.work_dir).free - free_before_start

    def print_metrics(self: 'CIEnvironment',
                      metrics_file: 'StringIO | None') -> 'None':
        """Write out a line with the metrics for this environment.

        If this environment was not found on disk, don't write anything.
        """
        if metrics_file is not None and self.work_dir.exists():
            print(self.name, self.cleanup_duration, self.num_deleted_symlinks,
                  self.freed_bytes, self.initial_disk_free_bytes,
                  file=metrics_file)


def delete_old_builds(symlinks: 'Iterable[SymlinkWithInfo]',
                      maximum_age_days: 'float', *, dry_run: 'bool') \
                      -> 'list[SymlinkWithInfo]':
    """Delete symlinks to builds older than the given cutoff.

    No cleanup of builds themselves is done; only symlinks are deleted.

    Return a list of remaining symlinks (i.e. those newer than the cutoff).
    """
    # Take out all the symlinks older than the requested age and delete
    # them from disk, leaving only more recent symlinks in the list.
    date_cutoff = time() - maximum_age_days * 24 * 60 * 60
    # We need to store items in a list because groupby throws away previous
    # groups when the next one is fetched.
    old: 'list[SymlinkWithInfo]' = []
    recent: 'list[SymlinkWithInfo]' = []
    for is_old, items in groupby(symlinks, key=lambda s: s[0] < date_cutoff):
        (old if is_old else recent).extend(items)

    LOGGER.debug("deleting %d symlinks older than cutoff", len(old))
    for _, to_delete, ci_env in old:
        ci_env.delete_symlink(to_delete, "older than cutoff", dry_run=dry_run)

    LOGGER.debug("%d symlinks newer than cutoff remain", len(recent))
    return recent


def cleanup_to_disk_threshold(symlinks: 'list[SymlinkWithInfo]',
                              ci_root: 'Path', free_bytes_target: 'int',
                              *, dry_run: 'bool') -> 'None':
    """Cleanup old builds until the given free space threshold is met."""
    def done():
        gibibyte = 1024 ** 3
        disk_target = free_bytes_target * gibibyte
        disk_free = disk_usage(ci_root).free
        LOGGER.debug(
            "%d symlinks remain; %.02f GiB free; target %.2f GiB",
            len(symlinks), disk_free / gibibyte, disk_target / gibibyte,
        )
        # If no symlinks are left to delete, or we've satisfied the disk
        # space threshold, we're done.
        return not symlinks or disk_free >= disk_target

    if dry_run:
        LOGGER.warning(
            "dry-run enabled and not keeping track of how much disk space "
            "would be freed by deleting old builds; if disk threshold is "
            "not met, every remaining build will show up as to be deleted"
        )

    while not done():
        _, to_delete, ci_env = symlinks.pop()
        ci_env.delete_symlink(to_delete, "free space less than threshold",
                              dry_run=dry_run)
        ci_env.cleanup(dry_run=dry_run)


def parse_devel_pkg_spec(devel_pkgs: 'str') -> 'frozenset[str]':
    """Parse development package spec, as used in CI.

    The spec is a string of newline-separated records, each consisting of
    whitespace separated fields indicating the GitHub repository of the
    package, a branch name to check out and (optionally, if different from the
    repository name) the aliBuild package name.
    """
    packages = set()
    for line in devel_pkgs.splitlines():
        try:
            # Three fields must be repo, branch, package name.
            _, _, pkgname = line.split()
        except ValueError:
            # Either one field (repo) or two fields (repo and branch name).
            repo, *_ = line.split()
            # Derive package name from the repo name (in owner/package format).
            _, is_valid, pkgname = repo.partition("/")
            if not is_valid or not pkgname:
                LOGGER.error("ignoring invalid dev pkg spec line: %r", line)
                continue
        packages.add(pkgname)
    return frozenset(packages)


def parse_args() -> 'Namespace':
    """Parse and return command-line arguments."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="only print what would be deleted")
    parser.add_argument(
        "-o", "--metrics-file", metavar="FILE", type=FileType("w"), help="""\
        print statistics to %(metavar)s; one line per CI build directory (use
        '-' for stdout); lines are of the format '{env_name} {duration_sec}
        {num_builds_deleted} {kib_freed_approx} {kib_avail_after}'
        """)
    parser.add_argument("-r", "--ci-root", type=Path, default=".", help="""\
    the directory containing build directories named after .env files (from
    DEFS_DIR), and aliBuild work directories named WORK_DIR underneath those
    """)
    parser.add_argument("-w", "--work-dir", default="sw", help="""\
    name of the aliBuild work directory under each CI build directory (default
    %(default)r)
    """)

    envs = parser.add_argument_group("select CI environments", description="""\
    Select the .env files for a specific CI builder. These are stored in a
    directory hierarchy like <DEFS_DIR>/<mesos_role>/<container_name>/*.env.
    The names of build directories and any development packages will be taken
    from these .env files, in order to clean up only development packages
    (leaving common build dependencies cached).
    """)
    envs.add_argument("-d", "--definitions-dir", metavar="DEFS_DIR", type=Path,
                      default="ali-bot/ci/repo-config", help="""\
                      root directory under which .env files are located in a
                      hierarchy (default %(default)r)
                      """)
    envs.add_argument("mesos_role", help="mesos role of the current CI worker")
    envs.add_argument("container_name", help=("short name of the container "
                                              "we're running in (e.g. slc8)"))

    select = parser.add_argument_group("select packages", description="""\
    If either of the option selecting specific packages is given, only clean up
    old builds of the specified packages. If neither option is given, or one is
    given but empty, clean up every package.
    """)
    select.add_argument(
        "-t", "--maximum-age", metavar="TIME", type=float,
        help="delete builds older than this many days (may be fractional)")
    select.add_argument(
        "-f", "--minimum-disk-space", metavar="DISK", type=float,
        help="""\
        delete old builds until at least this many gibibytes of disk space are
        free (may be fractional)
        """)

    return parser.parse_args()


def setup_logging() -> 'None':
    """Set up the general logger for the script."""
    LOGGER.setLevel(logging.DEBUG)
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(
        fmt="%(filename)s: %(levelname)s: %(message)s",
    ))
    LOGGER.addHandler(handler)


if __name__ == '__main__':
    setup_logging()
    main(parse_args())
