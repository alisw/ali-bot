#!/usr/bin/env python3

"""Cache CCDB objects by storing them in CVMFS.

When objects are cached in CVMFS, CCDB will link to their paths, so that
analysis tasks can read the objects directly from CVMFS for a significant
speed-up. This also reduces the load on CCDB.
"""

import argparse
import logging
import pathlib
from subprocess import check_call
import sys
import typing
import requests

LOG = logging.Logger(__name__)
ALIEN_REWRITE_PREFIX = "alien:///alice/data/CCDB/"


def main(args: argparse.Namespace) -> int:
    """Script entry point."""
    LOG.setLevel(logging.DEBUG)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    handler.setLevel(logging.DEBUG)
    LOG.addHandler(handler)
    cvmfs_prefix = pathlib.Path("/cvmfs") / args.cvmfs_repository / args.cvmfs_path
    # We expect files to be named after their CCDB GUIDs. Paths may be
    # arbitrarily deep.
    have_guids = {f.name for f in cvmfs_prefix.glob("**/????????-????-????-????-????????????")}
    new_objects = False
    ok = True
    if args.test_urls:
        pass
    elif args.dry_run:
        LOG.debug("would open CVMFS transaction")
    else:
        LOG.debug("opening CVMFS transaction")
        check_call(("cvmfs_server", "transaction", args.cvmfs_repository))
    try:
        with requests.Session() as session:
            for lineno0, ccdb_url in enumerate(args.ccdb_urls_file):
                comment_start = ccdb_url.find("#")
                if comment_start != -1:
                    ccdb_url = ccdb_url[:comment_start]
                ccdb_url = ccdb_url.strip()
                if not ccdb_url:
                    continue
                if args.test_urls:
                    ok &= test_object(session, args.ccdb_urls_file.name,
                                      lineno0 + 1, ccdb_url)
                    continue
                new_guid = store_object(ccdb_url, session, cvmfs_prefix,
                                        have_guids, dry_run=args.dry_run)
                if new_guid:
                    new_objects = True
                    have_guids.add(new_guid)
    except Exception as exc:
        if args.test_urls:
            pass
        elif args.dry_run:
            LOG.fatal("would abort CVMFS transaction due to:", exc_info=exc)
        else:
            LOG.fatal("aborting CVMFS transaction due to:", exc_info=exc)
            check_call(("cvmfs_server", "abort", "-f", args.cvmfs_repository))
        raise
    else:
        if args.test_urls:
            pass
        elif args.dry_run:
            LOG.debug("would publish CVMFS transaction")
        elif not new_objects:
            # We mustn't create too many transactions, so abort empty ones.
            LOG.debug("aborting transaction as there is nothing to publish")
            check_call(("cvmfs_server", "abort", "-f", args.cvmfs_repository))
        else:
            LOG.debug("publishing CVMFS transaction")
            check_call(("cvmfs_server", "publish", args.cvmfs_repository))
    return 0 if ok else 1


def store_object(ccdb_url: str,
                 session: requests.Session,
                 cvmfs_prefix: pathlib.Path,
                 have_guids: typing.Iterable[str],
                 *, dry_run: bool) -> 'str | None':
    """Fetch the given object from CCDB if needed and store it in CVMFS.

    If we already have the GUID that the given CCDB_URL points to (i.e., it is
    in HAVE_GUIDS), do nothing. If we don't have it yet, write it to CVMFS.

    Any failures will cause this object to be skipped (i.e. nothing will be
    written to CVMFS), and an error message to be logged.

    Return the newly cached object's GUID, or None if nothing was cached.
    """
    caching_headers = {"If-None-Match": ", ".join(have_guids)}
    # We only need the Content-Location header, so a HEAD request is enough.
    # requests.head() doesn't follow redirects by default, which is good.
    with session.head(ccdb_url, headers=caching_headers) as resp:
        status_code = resp.status_code
        locations = resp.headers.get("Content-Location", "").split(", ")
    if status_code == requests.codes.SEE_OTHER:
        # We don't have this object yet (or the underlying object has been
        # edited, so we need to cache the new object and GUID).
        LOG.debug("caching new object: %s", ccdb_url)
        # We expect a http:// Content-Location so we can fetch the object's
        # data. Getting it though alien:// would be much more effort.
        try:
            download_url = next(loc for loc in locations
                                if loc.startswith("http:"))
        except StopIteration:
            LOG.error("no http:// location for %s; skipping", ccdb_url)
            return None
        # To get the object's path on CVMFS, get the appropriate suffix of the
        # alien:// location.
        try:
            cvmfs_path = cvmfs_prefix / next(
                location[len(ALIEN_REWRITE_PREFIX):].strip("/")
                for location in locations
                if location.startswith(ALIEN_REWRITE_PREFIX)
            )
        except StopIteration:
            LOG.error("no usable alien:// location for %s; skipping", ccdb_url)
            return None
        # Now fetch the actual data and store it in CVMFS.
        with session.get(download_url, stream=True) as obj_resp:
            if dry_run and obj_resp.status_code == requests.codes.OK:
                LOG.info("fetched %s OK (%s; %s bytes); CVMFS not changed",
                         ccdb_url, cvmfs_path,
                         obj_resp.headers.get("Content-Length", "<unknown>"))
            elif obj_resp.status_code == requests.codes.OK:
                cvmfs_path.parent.mkdir(parents=True, exist_ok=True)
                with cvmfs_path.open("wb") as out_file:
                    for block in obj_resp.iter_content():
                        out_file.write(block)
                LOG.info("successfully fetched %s => %s (%d bytes)",
                         ccdb_url, cvmfs_path, cvmfs_path.stat().st_size)
                # The filename is the GUID of the object we've just cached.
                # Store it so we don't cache the same thing twice.
                return cvmfs_path.name
            else:
                LOG.error("got HTTP status %d when fetching %s; skipping",
                          obj_resp.status_code, download_url)
    elif status_code == requests.codes.NOT_MODIFIED:
        LOG.debug("object already cached and not modified: %s", ccdb_url)
    else:
        LOG.error("unhandled status code %d from CCDB for %s; skipping",
                  status_code, ccdb_url)
    return None


def parse_args() -> argparse.Namespace:
    """Parse and return command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="do not update CVMFS, only show what would be done")
    parser.add_argument("-t", "--test-urls", action="store_true",
                        help="only perform sanity checks on URLs in URLS_FILE")
    parser.add_argument("--cvmfs-repository", default="alice.cern.ch",
                        help="use a different CVMFS repository, assumed to be "
                        "present under /cvmfs (default %(default)s)")
    parser.add_argument("--cvmfs-path", default="ccdb",
                        help="specify the path inside the CVMFS repository to "
                        "use; will be created if needed (default %(default)s)")
    parser.add_argument("ccdb_urls_file", metavar="URLS_FILE",
                        type=argparse.FileType("r"),
                        help="file declaring CCDB objects to store in CVMFS, "
                        "one URL per line (default %(default)s)")
    return parser.parse_args()


def test_object(session: requests.Session, filename: str, lineno: int,
                ccdb_url: str) -> bool:
    """Perform sanity checks on the given URL.

    If a check fails, print a GitHub Actions-compatible error message and
    return False. Return True if everything passed.
    """
    def error(title, message):
        LOG.error("%s:%d: %s: %s", filename, lineno, title, message)
        print(f"::error file={filename},line={lineno},title={title}::{message}")

    if not ccdb_url.startswith("http://alice-ccdb.cern.ch/"):
        error("Invalid URL", "URLs must start with http://alice-ccdb.cern.ch/")
        return False

    with session.head(ccdb_url) as resp:
        LOG.debug("HEAD %s returned HTTP %d", ccdb_url, resp.status_code)
        if resp.status_code != requests.codes.SEE_OTHER:
            error("Unexpected return code", "CCDB replied with HTTP "
                  f"{resp.status_code}, but 303 was expected. Make sure the "
                  "URL points to an object, not a download or browse page.")
            return False
        locations = resp.headers.get("Content-Location", "").split(", ")
    LOG.debug("Content-Locations for %s: %r", ccdb_url, locations)

    ok = True
    if not any(loc.startswith("http://") for loc in locations):
        error("No http:// location", "CCDB returned no HTTP location for this "
              "object, so we cannot download it.")
        ok = False
    if not any(loc.startswith(ALIEN_REWRITE_PREFIX) for loc in locations):
        error("No suitable alien:// location", "CCDB returned no suitable "
              "AliEn location for this object. This is needed to calculate "
              "the correct path on CVMFS.")
        ok = False
    return ok


if __name__ == "__main__":
    sys.exit(main(parse_args()))
