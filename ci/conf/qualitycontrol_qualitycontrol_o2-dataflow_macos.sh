# Continuous builder configuration for O2 CI

# GitHub slug of the repository accepting PRs
PR_REPO=AliceO2Group/QualityControl

# What is the package to rebuild
PACKAGE=QualityControl

# How to name the check
CHECK_NAME=build/QualityControl/o2-dataflow/macOS

# PRs are made to this branch
PR_BRANCH=master

# What is the default to use
ALIBUILD_DEFAULTS=o2-dataflow

# Start PR check if PR comes from one of them (comma-separated)
TRUSTED_USERS=

# Start PR if author has already contributed to the PR
TRUST_COLLABORATORS=true

# Do not use comments in PRs (use Details instead)
DONT_USE_COMMENTS=1

# How many cores to use for parallel builds
JOBS=4