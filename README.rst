A collection of utilities used for ALICE continuous integration.

These are usually needed by our CI infrastructure and there is no particular need for a non-power-user to install it.

In case you are a power user debugging CI issues, you need to:

* Install `ali-bot` via (e.g.)::

    sudo pip install git+https://github.com/alisw/ali-bot

* Make sure you have a github token in a environment variable called `GITHUB_TOKEN`.
