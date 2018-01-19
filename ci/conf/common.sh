# Continuous builder configuration for Common: rebuild O2 whenever Common changes

# GitHub slug of the repository accepting PRs
PR_REPO=AliceO2Group/Common

# What is the directory where we clone the above repo
PR_REPO_CHECKOUT=Common

# What is the package to rebuild
PACKAGE=O2

# How to name the check
CHECK_NAME=build/O2/Common/macos

# PRs are made to this branch
PR_BRANCH=master

# What is the default to use
ALIBUILD_DEFAULTS=o2
