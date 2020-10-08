#!/usr/bin/env python3

'''Determines which RPMs in a directory are unneeded.'''

import argparse
import re
import os
import shlex
import sys
from functools import lru_cache
from pathlib import Path
from subprocess import run, DEVNULL, PIPE, CalledProcessError
from typing import Dict, Set, FrozenSet, Iterator, Tuple


# The following packages are ignored as we don't have the RPMs anyway and
# looking up their names here is quicker than searching for the RPM each time
# something depends on them. Note that adding a package here will SCHEDULE ITS
# RPM FOR DELETION if we do have it, even if important packages depend on it!
IGNORED_PACKAGES = (
    'environment-modules',
    'glfw',
    'glibc-headers',
    'libhugetlbfs-utils',
    'pda-kadapter-dkms',
)


# Cache results so querying frequently-depended-upon RPMs is fast.
@lru_cache(maxsize=None)
def get_dependencies(rpm_fname: str, rpm_dir: Path) -> Iterator[str]:
    '''Find direct dependencies of the given RPM.'''
    try:
        cmd = run(('rpm', '-qRp', rpm_fname),
                  stdin=DEVNULL, stderr=DEVNULL, stdout=PIPE, check=True)
    except CalledProcessError:
        print('WARN: in call to rpm: RPM likely not found or invalid:',
              rpm_fname, file=sys.stderr)
        return

    for depspec in cmd.stdout.strip().split(b'\n'):
        package, *extra = depspec.decode('utf-8').split()
        if package.startswith('/') or \
           package.startswith('rpmlib(') or \
           package in IGNORED_PACKAGES or \
           not any(rpm_dir.glob(f'{package}-*.x86_64.rpm')):
            # We don't have the RPM for this package, so there's nothing to
            # delete anyway. These seem to be special dependencies: specific
            # files (like /bin/sh), RPM features or system packages. Ignore.
            print(f'INFO: ignored package {package}, seems to be special',
                  file=sys.stderr)
            continue

        # Almost all packages just have version "1-1.el7", with the actual
        # version in the package name. The only exception is some packages in
        # AliBI/ and alisw-aliswmod, which has versions 1-1.el7 and 2-1.el7.
        # For alisw-aliswmod, packages seem to either depend on >= 2-1.el7 or
        # have no version expression.
        if package == 'alisw-aliswmod' and (not extra or extra == ['>=', '2']):
            # Some packages depend on alisw-aliswmod but do not give a version
            # expression, which presumably means any version. In that case,
            # just keep v2, the latest.
            yield str(rpm_dir / f'{package}-2-1.el7.x86_64.rpm')
        elif extra and extra[0] in ('=', '<='):
            # Use given version -- we want this or less, so use this.
            yield str(rpm_dir / f'{package}-{extra[1]}.x86_64.rpm')
        elif not extra or extra[0] == '>=':
            # No dependency expression or want latest; find all versions and
            # keep the newest. All valid versions I've found so far start with
            # a number. [0-9]* catches them all.
            all_versions = sorted(rpm_dir.glob(f'{package}-[0-9]*.x86_64.rpm'))
            if not all_versions:
                raise ValueError(f'expected RPM for {package} but none found!')
            yield str(all_versions[-1])
        else:
            raise ValueError(
                f'unexpected dependency expression {extra} for {package} '
                f'in {depspec.decode("utf-8")} of {rpm_fname}')


def build_dependency_tree(rpm_dir: Path) -> Dict[str, FrozenSet[str]]:
    '''Map each RPM name to its recursive dependencies.'''
    def resolve_deps_recursively(rpm: str, seen: Tuple[str, ...] = ()) \
            -> Iterator[str]:
        '''Search recursively for all dependencies of rpm.'''
        yield rpm
        # We keep track of already-seen dependencies up the subtree for this
        # package so we don't get into circular dependency loops.
        seen += (rpm,)
        for dep in get_dependencies(rpm, rpm_dir):
            if dep not in seen:
                yield from resolve_deps_recursively(dep, seen)

    return {rpm_file: frozenset(resolve_deps_recursively(rpm_file))
            for rpm_file in map(str, rpm_dir.glob('*.rpm'))}


def main(args: argparse.Namespace) -> None:
    '''Application entry point.'''
    tree = build_dependency_tree(args.rpm_dir)

    # Work out which toplevel packages must be kept.
    if args.keep_glob is not None:
        keep_toplevel = {str(p) for p in args.rpm_dir.glob(args.keep_glob)}
    elif args.keep_regex is not None:
        keep_toplevel = {str(p) for p in args.rpm_dir.iterdir()
                         if re.match(args.keep_regex, str(p))}
    elif args.keep_files is not None:
        keep_toplevel = set(args.keep_files)
    else:
        raise ValueError('one of -r, -g, -f must be specified')

    # Combine all toplevel packages' dependency trees.
    needed_rpms: Set[str] = set()
    for toplevel in keep_toplevel:
        needed_rpms |= tree[toplevel]

    # Work out which packages depend on the ones we want to keep; we want to
    # keep those too!
    for package, dependencies in tree.items():
        if not keep_toplevel.isdisjoint(dependencies):
            needed_rpms.add(package)

    # Work out which RPMs are not depended upon by things we want to keep.
    all_rpms = set(map(str, args.rpm_dir.glob('*.rpm')))
    rpms_to_remove = all_rpms - needed_rpms
    for rpm in rpms_to_remove:
        if args.do_delete:
            os.remove(rpm)
            print('INFO: deleted RPM', rpm, file=sys.stderr)
        else:
            print('rm', shlex.quote(rpm))


def parse_args() -> argparse.Namespace:
    '''Parse command-line arguments.'''
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--do-delete', action='store_true', help='actually delete orphan RPMs')
    parser.add_argument(
        '-d', '--rpm-dir', metavar='DIR', type=Path, default=Path('.'),
        help='directory containing RPMs to be checked; default %(default)s')
    keep_group = parser.add_mutually_exclusive_group(required=True)
    keep_group.add_argument(
        '-r', '--keep-regex', metavar='REGEX',
        help='keep RPMs that match REGEX and their dependencies')
    keep_group.add_argument(
        '-g', '--keep-glob', metavar='GLOB',
        help='keep RPMs that match GLOB and their dependencies')
    keep_group.add_argument(
        '-f', '--keep-files', metavar='FILE', nargs='+',
        help='keep the individually specified RPMs and their dependencies')
    return parser.parse_args()


if __name__ == '__main__':
    main(parse_args())
