# Continuous builder configuration for AliRoot with ROOT6

# GitHub slug of the repository accepting PRs
PR_REPO=alisw/AliRoot

# Where to check out that repository (build $PACKAGE with changes from $PR_REPO)
PR_REPO_CHECKOUT=AliRoot

# What is the package to rebuild
PACKAGE=AliRoot-guntest

# How to name the check
CHECK_NAME=build/AliRoot/macos
#CHECK_NAME=build/AliRoot/AliRoot-guntest/next-root6/macOS

# PRs are made to this branch
PR_BRANCH=master

# What is the default to use
ALIBUILD_DEFAULTS=next-root6

# Start PR check if PR comes from one of them (comma-separated)
TRUSTED_USERS=

# Start PR if author has already contributed to the PR
TRUST_COLLABORATORS=

# How many cores to use for parallel builds
JOBS=6