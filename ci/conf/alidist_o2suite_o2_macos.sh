# Continuous builder configuration for alidist

# GitHub slug of the repository accepting PRs
PR_REPO=alisw/alidist

# Where to checkout the PR repo (mandatory, otherwise will use PACKAGE)
PR_REPO_CHECKOUT=alidist

# What is the package to rebuild
PACKAGE=O2Suite

# How to name the check
CHECK_NAME=build/alidist/O2Suite/o2/macOS

# PRs are made to this branch
PR_BRANCH=master

# What is the default to use
ALIBUILD_DEFAULTS=o2

# Start PR check if PR comes from one of them (comma-separated)
TRUSTED_USERS=

# Start PR if author has already contributed to the PR
TRUST_COLLABORATORS=true

# Do not use comments in PRs (use Details instead)
DONT_USE_COMMENTS=1

# Extra repositories to download
EXTRA_REPOS=( "repo=AliceO2Group/AliceO2 branch=dev checkout=O2" )

# How many cores to use for parallel builds
JOBS=8