# Continuous builder configuration for AliRoot on SLC6 native

# What is the package to rebuild
PACKAGE=AliRoot

# How many parallel jobs (i.e. make -j$JOBS)
JOBS=4

# Pause between each PR processing (seconds)
DELAY=150

# Where to get/push cached builds from/to
REMOTE_STORE="rsync://repo-ci.marathon.mesos/store/::rw"

# GitHub slug of the repository accepting PRs
PR_REPO=alisw/AliRoot

# PRs are made to this branch
PR_BRANCH=master

# Start PR check if PR comes from one of them (comma-separated)
TRUSTED_USERS=

# Start PR if author has already contributed to the PR
TRUST_COLLABORATORS=

# What is the default to use
ALIBUILD_DEFAULTS=el6native

# How to name the check
CHECK_NAME=build/AliRoot/el6native
