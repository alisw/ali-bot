#!/usr/bin/env python3

'''Determines which RPMs in a directory are unneeded.'''

import argparse
import os
import shlex
import sys
from functools import lru_cache
from pathlib import Path
from subprocess import run, DEVNULL, PIPE, CalledProcessError
from typing import List


# Cache results so querying frequently-depended-upon RPMs is fast.
@lru_cache(maxsize=None)
def get_dependencies(rpm_fname: str, rpm_dir: Path) -> List[str]:
    '''Recursively create dependency tree for an RPM file.

    For convenience, includes its own name in the list of its dependencies.
    '''
    try:
        cmd = run(('rpm', '-qR', '--recommends', '--suggests', rpm_fname),
                  stdin=DEVNULL, stderr=DEVNULL, stdout=PIPE, check=True)
    except CalledProcessError:
        print('ERROR: RPM not found or invalid:', rpm_fname, file=sys.stderr)
        return [rpm_fname]

    dependencies = [rpm_fname]
    for depspec in cmd.stdout.strip().split(b'\n'):
        package, *extra = depspec.decode('utf-8').split()
        if package.startswith('/') or package.startswith('rpmlib('):
            # These seem to be special dependencies: specific files (like
            # /bin/sh) and RPM features. Ignore them.
            continue
        if not any(rpm_dir.glob(f'{package}-*.x86_64.rpm')):
            # We don't have the RPM for this package, so there's nothing to
            # delete anyway. Ignore it.
            print(f'INFO: ignoring {package}, RPM not found', file=sys.stderr)
            continue

        if package == 'alisw-aliswmod' and not extra:
            # Some packages depend on alisw-aliswmod but do not give a version
            # expression, which presumably means any version. In that case,
            # just keep v2, the latest.
            version = '2-1.el7'
        elif extra:
            operator, version = extra

            # Almost all packages just have version "1-1.el7", with the actual
            # version in the package name. The only exception is some packages
            # in AliBI/ and alisw-aliswmod, which has versions 1-1.el7 and
            # 2-1.el7. For alisw-aliswmod, packages seem to either depend on >=
            # 2-1.el7 or have no version expression (see above).
            if package == 'alisw-aliswmod' and operator == '>=' and version == '2':
                # Convert to an actual version string found in RPM file names.
                version = '2-1.el7'
            elif operator == '=' and version == '1-1.el7':
                # Nothing to do, but don't raise a ValueError.
                pass
            else:
                raise ValueError('unexpected dependency expression',
                                 rpm_fname, package, extra)
        else:
            raise ValueError('expected dependency expression for pkg', package)

        # The dependency itself is included in the list returned by the
        # following call.
        dependencies.extend(get_dependencies(
            str(rpm_dir / f'{package}-{version}.x86_64.rpm'), rpm_dir))

    return dependencies


def main(args: argparse.Namespace) -> None:
    '''Application entry point.'''
    # Keep any RPM with "O2" in the name. "o2" (lowercase) is not used in
    # package names except in those that also contain "O2" (uppercase).
    keep_toplevel_rpms = map(str, args.rpm_dir.glob('*O2*.rpm'))

    # Combine all toplevel packages' dependency trees.
    needed_rpms = set()
    for toplevel in keep_toplevel_rpms:
        needed_rpms |= set(get_dependencies(toplevel, args.rpm_dir))

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
        'rpm_dir', nargs='?', metavar='DIR', type=Path, default=Path('.'),
        help='directory containing RPMs to be checked; default %(default)s')
    return parser.parse_args()


if __name__ == '__main__':
    main(parse_args())
