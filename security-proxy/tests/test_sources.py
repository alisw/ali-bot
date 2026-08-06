"""Bootstrap sources are declarative: they name what to read, never a command to run.

The `command` source was removed in 2026-08. The bootstrap config is writable by
anything running as the user, so a shell source was arbitrary code execution as the
user at the next bootstrap -- the same-uid problem attended slots exist to contain,
arriving through a door they do not cover. These tests pin the replacements (`file`,
`vault`, `device_authorize`) and that the old kind stays rejected with a migration hint.
"""
import http.server
import json
import socket as _socket
import sys
import tempfile
import threading
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


def _fake_vault(tmp):
    """A stand-in for the proxy's vault route plus its agent socket.

    Proves the wiring -- gate token in, field out -- without touching real Vault.
    """
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            token = self.headers.get("X-Vault-Token")
            body = json.dumps({"data": {"data": {"gitlab_pass": f"SECRET-for-{token}"}}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    sock_path = tmp / "agent.sock"

    def serve_agent():
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.bind(str(sock_path))
        s.listen(4)
        while True:
            conn, _ = s.accept()
            conn.recv(4096)
            conn.sendall((json.dumps({"port": port, "service": "vault",
                                      "token": "GATE"}) + "\n").encode())
            conn.close()

    threading.Thread(target=serve_agent, daemon=True).start()
    for _ in range(50):                      # wait for the socket to appear
        if sock_path.exists():
            break
        time.sleep(0.02)
    return sock_path


def run():
    tmp = Path(tempfile.mkdtemp(prefix="security-proxy-tests-"))

    # --- file: replaces `cat a b` -------------------------------------------
    (tmp / "cert.pem").write_text("CERT\n")
    (tmp / "key.pem").write_text("KEY\n")
    check("file, single path", sp._resolve_source({"file": str(tmp / "cert.pem")}), "CERT\n")
    check("file, several concatenated",
          sp._resolve_source({"file": [str(tmp / "cert.pem"), str(tmp / "key.pem")]}),
          "CERT\nKEY\n")

    # --- the removed kind ----------------------------------------------------
    try:
        sp._resolve_source({"command": "echo pwned"})
        check("command source rejected", "accepted", "rejected")
    except ValueError as e:
        check("command source rejected with a migration hint",
              "removed" in str(e) and "arbitrary shell" in str(e), True)

    # --- malformed sources ---------------------------------------------------
    for bad, label in [({}, "empty source rejected"),
                       ({"nope": 1}, "unknown kind rejected"),
                       ({"file": "x", "keychain": "y"}, "two kinds at once rejected")]:
        try:
            sp._resolve_source(bad)
            check(label, "accepted", "rejected")
        except ValueError:
            check(label, "rejected", "rejected")

    # --- vault ---------------------------------------------------------------
    try:
        sp._resolve_source({"vault": {"path": "kv/data/ci", "field": "x"}}, None)
        check("vault without an agent socket is refused", "accepted", "rejected")
    except RuntimeError as e:
        check("vault without an agent socket names the missing key", "agent_socket" in str(e), True)

    sock = _fake_vault(tmp)
    check("vault reads its field through the proxy (KV v2 shape)",
          sp._resolve_source({"vault": {"path": "kv/data/ci", "field": "gitlab_pass"}}, sock),
          "SECRET-for-GATE")
    try:
        sp._resolve_source({"vault": {"path": "kv/data/ci", "field": "absent"}}, sock)
        check("missing vault field is an error", "accepted", "rejected")
    except RuntimeError as e:
        check("missing vault field says what the secret does have",
              "gitlab_pass" in str(e), True)

    # --- the real map, when there is one (skipped on a fresh machine) --------
    real = Path("~/.security-proxy-bootstrap.json").expanduser()
    if real.exists():
        cfg = json.load(open(real))
        slots = cfg.get("slots", {})
        check("every slot in the real map has exactly one known kind",
              all(len([k for k in sp.SOURCE_KINDS if k in src]) == 1
                  for src in slots.values()), True)
        check("no command source left in the real map",
              any("command" in src for src in slots.values()), False)
        for slot, src in (cfg.get("attended") or {}).items():
            kc = src.get("keychain")
            check(f"attended {slot!r} is sourced from an explicit locked keychain",
                  isinstance(kc, dict) and bool(kc.get("keychain")), True)
    else:
        print("  skip  no ~/.security-proxy-bootstrap.json on this machine")

    return failures


if __name__ == "__main__":
    print(__doc__.splitlines()[0])
    bad = run()
    print(f"{len(bad)} failure(s)")
    for f in bad:
        print(f"  FAILED: {f}")
    sys.exit(1 if bad else 0)
