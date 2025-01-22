#!/usr/bin/env python
""" Package alibuild using setuptools
"""

import sys
# Always prefer setuptools over distutils
from setuptools import setup, find_packages
# To use a consistent encoding
from codecs import open
from os import path

here = path.abspath(path.dirname(__file__))

# Get the long description from the README file
with open(path.join(here, 'README.rst'), encoding='utf-8') as f:
    long_description = f.read()

install_requires = ['PyGithub==1.45', 'argparse', 'requests', 'pytz', 's3cmd',
                    'pyyaml']
# Old setuptools versions (which pip2 uses) don't support range comparisons
# (like :python_version >= "3.6") in extras_require, so do this ourselves here.
if sys.version_info >= (3, 8):
    # Older boto3 versions are incompatible with newer Python versions,
    # specifically the newer urllib3 that comes with newer Python versions.
    install_requires.append('boto3==1.35.95')
elif sys.version_info >= (3, 6):
    # This is the last version to support Python 3.6.
    install_requires.append('boto3==1.23.10')

setup(
    name='ali-bot',

    # Single-source our package version using setuptools_scm. This makes it
    # PEP440-compliant, and it always references the ali-bot commit that each
    # script was built from.
    use_scm_version=True,
    setup_requires=[
        # The 7.* series removed support for Python 3.6.
        'setuptools_scm<7.0.0' if sys.version_info < (3, 7) else
        'setuptools_scm'
    ] + ['packaging<=23'] if sys.version_info <(3, 7) else [],

    description='ALICE Multipurpose bot',
    long_description=long_description,

    # The project's main homepage.
    url='https://alisw.github.io/ali-bot',

    # Author details
    author='Giulio Eulisse',
    author_email='giulio.eulisse@cern.ch',

    # Choose your license
    license='GPL',

    # See https://pypi.python.org/pypi?%3Aaction=list_classifiers
    classifiers=[
        # How mature is this project? Common values are
        #   3 - Alpha
        #   4 - Beta
        #   5 - Production/Stable
        'Development Status :: 4 - Beta',

        # Indicate who your project is intended for
        'Intended Audience :: Developers',
        'Topic :: Software Development :: Build Tools',

        # Pick your license as you wish (should match "license" above)
        'License :: OSI Approved :: GNU General Public License v3 (GPLv3)',

        # Specify the Python versions you support here. In particular, ensure
        # that you indicate whether you support Python 2, Python 3 or both.
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.13',
    ],

    # What does your project relate to?
    keywords='HEP ALICE',

    # You can just specify the packages manually here if your project is
    # simple. Or you can use find_packages().
    packages=find_packages(),

    # Alternatively, if you want to distribute just a my_module.py, uncomment
    # this:
    #   py_modules=["my_module"],

    # List run-time dependencies here.  These will be installed by pip when
    # your project is installed. For an analysis of "install_requires" vs pip's
    # requirements files see:
    # https://packaging.python.org/en/latest/requirements.html
    install_requires=install_requires,

    # List additional groups of dependencies here (e.g. development
    # dependencies). You can install these using the following syntax,
    # for example:
    # $ pip install -e .[dev,test]
    extras_require={
        "services": ["Twisted", "klein", "python-ldap"],
        "ci": [
            "gql",
            "requests-toolbelt",  # for gql
            # for gql; by default it pulls in a version that isn't compatible with python3.6
            "typing-extensions==4.1.1; python_version == '3.6'",
        ],
        "utils": ["python-nomad"],
    },

    # If there are data files included in your packages that need to be
    # installed, specify them here.  If using Python 2.6 or less, then these
    # have to be included in MANIFEST.in as well.
    include_package_data=True,
    package_data={
    },

    # To provide executable scripts, use entry points in preference to the
    # "scripts" keyword. Entry points provide cross-platform support and allow
    # pip to create the appropriate form of executable for the target platform.
    scripts = [
        # Continuous Integration
        "set-github-status",
        "report-pr-errors",
        "list-branch-pr",
        "alidist-override-tags",
        # Analytics
        "analytics/report-analytics",
        "analytics/report-metric-monalisa",
        # Continuous Builders
        "ci/continuous-builder.sh",
        "ci/build-helpers.sh",
        "ci/build-loop.sh",
        "ci/cleanup.py",
        # GitHub API monitoring
        "monitor-github-api",
        "monitor-github-api-monalisa.sh",
        # S3 housekeeping
        "repo-s3-cleanup",
        # Check daily tags
        "check-daily-slack",
        "daily-tags.sh",
        "build-any-ib.sh",
        "build-package",
        # Wait for open Pull Requests before daily tags
        "ci/check-open-pr",
        # Process PR permissions
        "ci/process-pull-request-http.py",
        "ci/sync-egroups.py",
        "ci/sync-mapusers.py",
        # S3 repo maintenance
        "update-symlink-manifests",
        # Get PR information
        "ci/prinfo",
        # Helpers
        "ci-status-overview",
        "utils/bulk-change-pr-status",
        "utils/bulk-change-pr-status-by-checker",
        "utils/ciqueues",
        "utils/ci-status-history",
        "utils/duplicate-hash-tarballs",
        "utils/logspecs",
        "utils/logtimes",
        "utils/nomad-diskfree",
        "utils/pdci",
    ]
)
