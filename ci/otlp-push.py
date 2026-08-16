#!/usr/bin/env python3

"""Push gauge metrics to an OTLP/HTTP endpoint.

The command line mirrors influxdb_push in ci/build-helpers.sh, so call sites
read the same either way:

    otlp-push.py NAME TAG=V TAG=V -- FIELD=V FIELD=V

Each FIELD becomes its own metric, named NAME_FIELD, carrying every TAG as an
attribute. That split is not a stylistic choice: an InfluxDB point holds a set
of named fields, while a Prometheus series holds exactly one value, so one
influxdb_push turns into several metrics.

The endpoint comes from OTLP_METRICS_URL and the credential from
OTLP_WRITE_TOKEN -- from the environment, never argv, so neither can surface in
`ps` output or a shell trace. When writing through the security-proxy the token
is a rotating gate token and the proxy swaps in the real tenant credential.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request


def parse_pairs(args):
    """Split "NAME TAG=V ... -- FIELD=V ..." into (name, tags, fields)."""
    if not args:
        raise ValueError("no metric name given")
    name, rest = args[0], args[1:]
    try:
        sep = rest.index("--")
    except ValueError:
        raise ValueError('missing "--" separating tags from fields') from None
    tags, fields = rest[:sep], rest[sep + 1:]
    if not fields:
        raise ValueError("no fields given; there would be nothing to record")

    def to_dict(items, what):
        out = {}
        for item in items:
            key, eq, value = item.partition("=")
            if not eq or not key:
                raise ValueError(f"{what} {item!r} is not KEY=VALUE")
            out[key] = value
        return out

    return name, to_dict(tags, "tag"), to_dict(fields, "field")


def build_payload(name, tags, fields, now_ns):
    """Build an OTLP/JSON ExportMetricsServiceRequest of gauges."""
    attributes = [{"key": k, "value": {"stringValue": v}} for k, v in sorted(tags.items())]
    metrics = []
    for field, raw in sorted(fields.items()):
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(f"field {field}={raw!r} is not a number") from None
        metrics.append({
            "name": f"{name}_{field}",
            # Gauges, not sums: every one of these is a level observed now (how
            # many PRs are queued, how long the oldest has waited), never a
            # running count. Declaring a sum would make rate() meaningful when
            # it is not.
            "gauge": {"dataPoints": [{
                "timeUnixNano": str(now_ns),   # int64 is a string in OTLP/JSON
                "asDouble": value,
                "attributes": attributes,
            }]},
        })
    return {"resourceMetrics": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "ci-queue-metrics"}},
        ]},
        "scopeMetrics": [{
            "scope": {"name": "ali-bot/queue-metrics"},
            "metrics": metrics,
        }],
    }]}


def post(url, payload, token, timeout):
    """POST the payload, returning None on success or a message on failure."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(2048).decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read(512).decode("utf-8", "replace").strip()
        return f"{exc.code} {exc.reason}: {detail}"
    except OSError as exc:
        return str(exc)
    # A partial success is reported in the body with a 200, so it has to be
    # read rather than assumed from the status code.
    if body and body not in ("{}", '{"partialSuccess":{}}'):
        return f"endpoint reported: {body}"
    return None


USAGE = ("usage: otlp-push.py [--dry-run] [--timeout SECONDS] "
         "NAME TAG=V ... -- FIELD=V ...")


def main():
    # Hand-rolled rather than argparse: argparse consumes the "--" that
    # separates tags from fields, and that separator is the whole point of
    # matching influxdb_push's call signature.
    argv, dry_run, timeout = sys.argv[1:], False, 20.0
    while argv and argv[0] != "--" and argv[0].startswith("--"):
        option = argv.pop(0)
        if option in ("-h", "--help"):
            print(__doc__.strip(), "\n\n", USAGE, sep="")
            return
        if option == "--dry-run":
            dry_run = True
        elif option == "--timeout" and argv:
            timeout = float(argv.pop(0))
        else:
            sys.exit(f"otlp-push.py: unrecognised option {option!r}\n{USAGE}")

    try:
        name, tags, fields = parse_pairs(argv)
        payload = build_payload(name, tags, fields, time.time_ns())
    except ValueError as exc:
        sys.exit(f"otlp-push.py: error: {exc}\n{USAGE}")

    if dry_run:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    url = os.environ.get("OTLP_METRICS_URL")
    if not url:
        return   # not configured: same silent no-op as an empty INFLUXDB_WRITE_URL

    error = post(url, payload, os.environ.get("OTLP_WRITE_TOKEN"), timeout)
    if error:
        # Never echo the URL: through the proxy it is harmless, but run by hand
        # it may carry credentials, and this goes to a CI log.
        sys.exit(f"otlp-push.py: could not push {name}: {error}")


if __name__ == "__main__":
    main()
