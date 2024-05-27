# Declarative S3 cleanup script

This script is essentially the inverse of `aliPublish`: it takes a config with versions of packages it can delete, and then resolves dependencies and deletes anything that isn't still in use.

Run `./repo-s3-cleanup --help` to see options.

The basic operation to delete packages declared in `cleanup-rules.yaml` is:

```bash
./repo-s3-cleanup --do-it cleanup-rules.yaml
```

It is probably best to run this script manually when needed, e.g. when deleting new batches of old tags.
Then, update the configuration and run `repo-s3-cleanup` **without** the `-y`/`--do-it` option to see what would be deleted, until you're certain that everything is correct.

If a package is not listed for deletion, but it should be, look for `... blocked by ...` lines in the output.
They will tell you what still depends on the package you want to delete.
