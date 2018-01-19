# Continuous builder configuration for AliRoot with ROOT6

# GitHub slug of the repository accepting PRs
PR_REPO=alisw/AliRoot

# What is the package to rebuild
PACKAGE=AliRoot

# How to name the check
CHECK_NAME=build/AliRoot/macos

# PRs are made to this branch
PR_BRANCH=master

# What is the default to use
ALIBUILD_DEFAULTS=root6

# Start PR check if PR comes from one of them (comma-separated)
TRUSTED_USERS=

# Start PR if author has already contributed to the PR
TRUST_COLLABORATORS=
