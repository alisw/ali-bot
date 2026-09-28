"""A failed FIRST JAliEn mint must not cost half a day.

rebuild_tls() keeps the previous token in place when a re-mint fails, so waiting
out the full refresh interval is correct once a token is held. It is NOT correct
before the first successful mint: the proxy then answers
`{"error": "jalien token not available"}`, and ci-linux-x86*.nomad's entrypoint
refuses to start a worker without a token.

Measured on alissandra05 on 2026-09-23: one
`alien token: could not mint: [Errno 104] Connection reset by peer` from JAliEn
central left the node with no token, the only retry was the 12-hour refresh, and
the worker sat out of the CI pool until it was restarted by hand. The proxy logs
that line on STDOUT while uvicorn logs to stderr, so it is easy to miss.

So the loop has two regimes, and these are the rules it follows.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import security_proxy as sp  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


def run():
    failures.clear()
    interval = sp.DEFAULT_ALIEN_REFRESH_SECONDS
    lo, hi = sp.ALIEN_RETRY_MIN_SECONDS, sp.ALIEN_RETRY_MAX_SECONDS

    # --- which regime applies --------------------------------------------------
    check("holding a token, wait the full refresh interval",
          sp.alien_wait_for(True, interval, lo), interval)
    check("holding none, wait the short backoff instead",
          sp.alien_wait_for(False, interval, lo), lo)

    # --- how the backoff moves -------------------------------------------------
    check("a failure doubles it", sp.alien_backoff_after(False, lo), lo * 2)
    check("it is capped", sp.alien_backoff_after(False, hi), hi)
    b = lo
    for _ in range(12):
        b = sp.alien_backoff_after(False, b)
    check("repeated failure settles at the cap", b, hi)
    check("a success resets it", sp.alien_backoff_after(True, b), lo)

    # --- the requirement this exists for ---------------------------------------
    # alissandra05 lost half a day to ONE reset packet. A sustained outage may
    # legitimately back off towards the cap; a lone transient failure must not
    # cost more than seconds, so pin the FIRST retry rather than a cumulative sum.
    check("the first retry with no token is seconds away",
          sp.alien_wait_for(False, interval, lo) <= 10, True)
    b = lo
    for _ in range(4):
        b = sp.alien_backoff_after(False, b)
    check("after four failures the wait is still minutes, not hours",
          sp.alien_wait_for(False, interval, b) <= hi, True)
    check("the empty-token cap stays far below the refresh interval",
          hi * 12 < interval, True)

    return failures


if __name__ == "__main__":
    print(__doc__.splitlines()[0])
    bad = run()
    print(f"{'FAILED' if bad else 'passed'}: {len(bad)} failure(s)")
    sys.exit(1 if bad else 0)
