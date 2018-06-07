# Continuous builder configuration for O2 CI

# GitHub slug of the repository accepting PRs
PR_REPO=AliceO2Group/AliceO2

# What is the package to rebuild
PACKAGE=O2

# How to name the check
CHECK_NAME=build/o2/macos

# PRs are made to this branch
PR_BRANCH=dev

# What is the default to use
ALIBUILD_DEFAULTS=o2

# Start PR check if PR comes from one of them (comma-separated)
TRUSTED_USERS=

# Start PR if author has already contributed to the PR
TRUST_COLLABORATORS=true

# Do not use comments in PRs (use Details instead)
DONT_USE_COMMENTS=1
