aliPublish
==========

Installation
------------

Only once:

```bash
mkdir publisher
cd publisher
curl -LO https://raw.githubusercontent.com/alisw/ali-bot/master/publish/get-and-run.sh
chmod +x get-and-run.sh
```

The script is ready to be put in a crontab like this:

```
*/20 * * * *   /full/path/to/get-and-run.sh > /dev/null 2>&1
```

New versions of `aliPublish` and the configuration script are obtained
automatically before each run.
