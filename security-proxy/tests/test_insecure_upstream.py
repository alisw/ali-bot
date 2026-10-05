"""Route upstreams: http:// only with an explicit per-route "insecure_http_upstream".

Plain HTTP upstreams carry the injected credential in clear text, so they are
refused unless the route opts in. Each case runs main() as a subprocess with a
duplicate route name appended: that error is raised after every route's upstream
has been validated, so reaching it means validation passed -- without ever
starting the server.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROXY = Path(__file__).resolve().parent.parent / "security_proxy.py"

failures = []


def check(label, ok, detail=""):
    if not ok:
        failures.append(f"{label}: {detail}")
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")


def run_proxy(routes):
    routes = routes + [{"name": "dupe", "prefix": "/a/", "upstream": "https://example.cern.ch"},
                       {"name": "dupe", "prefix": "/b/", "upstream": "https://example.cern.ch"}]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"routes": routes}, f)
    p = subprocess.run([sys.executable, str(PROXY), "--config", f.name,
                        "--cert", "/dev/null", "--key", "/dev/null"],
                       capture_output=True, text=True, timeout=60)
    Path(f.name).unlink()
    return p.stdout + p.stderr


def run():
    out = run_proxy([{"name": "plain", "prefix": "/plain/", "upstream": "http://example.cern.ch"}])
    check("http:// refused without opt-in", "must use https://" in out, out[-300:])

    out = run_proxy([{"name": "plain", "prefix": "/plain/", "upstream": "http://example.cern.ch",
                      "insecure_http_upstream": True}])
    check("http:// accepted with opt-in", "duplicate route name" in out, out[-300:])
    check("opt-in warns at startup", "WITHOUT TLS" in out, out[-300:])

    out = run_proxy([{"name": "plain", "prefix": "/plain/", "upstream": "http://example.cern.ch",
                      "insecure_http_upstream": "yes"}])
    check("opt-in must be a boolean", "must be a boolean" in out, out[-300:])

    out = run_proxy([{"name": "tls", "prefix": "/tls/", "upstream": "https://example.cern.ch"}])
    check("https:// unaffected, no warning",
          "duplicate route name" in out and "WITHOUT TLS" not in out, out[-300:])

    out = run_proxy([{"name": "ws", "prefix": "/ws/", "upstream": "ws://example.cern.ch",
                      "websocket": True}])
    check("ws:// refused without opt-in", "must use" in out and "wss://" in out, out[-300:])
    return failures


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
