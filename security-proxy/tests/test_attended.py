"""Attended slots: a high-privilege secret is absent outside a window a human opened.

Covers the window mechanics -- use budget, TTL, the sweeper that drops an untouched
value on time, and the 403-vs-503 split that tells a caller *which* fault it hit.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import security_proxy as sp

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


def run():
    sp.ATTENDED.update({"admin": {"ttl": 300, "max_uses": 2},
                        "timed": {"ttl": 1, "max_uses": None}})

    # --- use budget ---------------------------------------------------------
    sp.SLOTS["admin"] = "s3cr3t"
    sp.slot_arm("admin")
    check("1st use served", sp.slot_get("admin"), "s3cr3t")
    check("2nd use served", sp.slot_get("admin"), "s3cr3t")
    check("3rd use denied once the budget is spent", sp.slot_get("admin"), None)
    check("value wiped from memory", sp.SLOTS.get("admin"), None)
    check("window state gone", sp.SLOT_STATE.get("admin"), None)

    # --- a non-request read must not spend the budget ------------------------
    sp.SLOTS["admin"] = "s3cr3t"
    sp.slot_arm("admin")
    sp.slot_get("admin", consume=False)
    sp.slot_get("admin", consume=False)
    check("consume=False leaves the budget intact", sp.SLOT_STATE["admin"]["uses_left"], 2)

    # --- ttl, on access ------------------------------------------------------
    sp.SLOTS["timed"] = "tmp"
    sp.slot_arm("timed")
    check("served inside the ttl", sp.slot_get("timed"), "tmp")
    sp.SLOT_STATE["timed"]["expires"] = time.monotonic() - 1
    check("denied past the ttl", sp.slot_get("timed"), None)
    check("wiped past the ttl", sp.SLOTS.get("timed"), None)

    # --- ttl, without access: the sweeper must still drop it -----------------
    sp.SLOTS["timed"] = "tmp"
    sp.slot_arm("timed")
    sp.SLOT_STATE["timed"]["expires"] = time.monotonic() - 1
    sp.sweep_attended_slots()
    check("sweeper wipes an untouched expired slot", sp.SLOTS.get("timed"), None)

    # --- a value with no window is refused, not served -----------------------
    sp.SLOTS["admin"] = "orphan"
    sp.SLOT_STATE.pop("admin", None)
    check("value armed-less is refused", sp.slot_get("admin"), None)

    # --- ordinary slots are untouched by any of this -------------------------
    sp.SLOTS["nomad"] = "plain"
    for _ in range(5):
        sp.slot_get("nomad")
    check("plain slot unaffected", sp.slot_get("nomad"), "plain")

    # --- error mapping: different fault, different fix ------------------------
    check("attended slot -> 403", sp.SlotUnavailable("admin").status, 403)
    check("unprovisioned slot -> 503", sp.SlotUnavailable("nomad").status, 503)
    check("403 detail names the unlock command",
          "security-proxy-unlock admin" in sp.SlotUnavailable("admin").detail, True)

    # --- a locked attended route raises, and raises as a 403 ------------------
    rt = sp.Route(prefix="/", upstream="https://example.invalid", token="t",
                  name="admin-route", ingest_headers={"X-Admin-Token": "admin"})
    try:
        sp.resolve_ingest_headers(rt)
        check("locked attended route raises", "no exception", "SlotUnavailable")
    except sp.SlotUnavailable as e:
        check("locked attended route raises 403", e.status, 403)

    return failures


if __name__ == "__main__":
    print(__doc__.splitlines()[0])
    bad = run()
    print(f"{len(bad)} failure(s)")
    for f in bad:
        print(f"  FAILED: {f}")
    sys.exit(1 if bad else 0)
