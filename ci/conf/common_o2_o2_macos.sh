# Continuous builder configuration for Common: rebuild O2 whenever Common changes

# GitHub slug of the repository accepting PRs
PR_REPO=AliceO2Group/Common

# What is the directory where we clone the above repo
PR_REPO_CHECKOUT=Common

# What is the package to rebuild
PACKAGE=O2

# How to name the check
CHECK_NAME=build/Common/O2/o2/macOS

# PRs are made to this branch
PR_BRANCH=master

# What is the default to use
ALIBUILD_DEFAULTS=o2

# Do not use comments in PRs (use Details instead)
DONT_USE_COMMENTS=1

# How many cores to use for parallel builds
JOBS=4