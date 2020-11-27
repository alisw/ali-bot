#!/usr/bin/env python
""" Package alibuild using setuptools
"""

# Always prefer setuptools over distutils
from setuptools import setup, find_packages
# To use a consistent encoding
from codecs import open
from os import path

here = path.abspath(path.dirname(__file__))

# Get the long description from the README file
with open(path.join(here, 'README.rst'), encoding='utf-8') as f:
    long_description = f.read()

setup(
    name='ali-bot',

    # Versions should comply with PEP440.  For a discussion on single-sourcing
    # the version across setup.py and the project code, see
    # https://packaging.python.org/en/latest/single_source_version.html
    #
    # LAST_TAG is actually a placeholder which will be automatically replaced by 
    # the release-alibuild pipeline in jenkins whenever we need a new release.
    version='LAST_TAG',

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
        'Programming Language :: Python :: 2.6',
        'Programming Language :: Python :: 2.7'
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
    install_requires=['PyGithub==1.45', 'argparse', 'requests', 'pytz',
                      'pytz', 'boto3', 's3cmd', 'pyyaml'],

    # List additional groups of dependencies here (e.g. development
    # dependencies). You can install these using the following syntax,
    # for example:
    # $ pip install -e .[dev,test]
    extras_require={
      "services": ["Twisted==18.9.0", "klein", "python-ldap"]
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
               # GitHub API monitoring
               "monitor-github-api",
               "monitor-github-api-monalisa.sh",
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
               # Get PR information
               "ci/prinfo",
               # Resolve Mesos DNS
               "mesos-dns-lookup",
               # Helpers
               "clean-repo-ci",
               "bulk-change-pr-status",
              ]
)
