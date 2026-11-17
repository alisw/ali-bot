"""A stand-in for MONIT's OTLP/HTTP endpoint, for the tests: it keeps every
POSTed body. points() flattens them to (metric name, {attribute: value}, value)."""
import http.server
import json
import os
import threading


def serve(port: int, out: list) -> http.server.ThreadingHTTPServer:
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            out.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if os.environ.get("FAKE_OTLP_DUMP"):     # every metric name seen, for checking a dashboard
                with open(os.environ["FAKE_OTLP_DUMP"], "a") as f:
                    f.writelines(n + "\n" for n, _, _ in points(out[-1:]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def points(bodies: list) -> list[tuple[str, dict, float]]:
    return [(m["name"], {a["key"]: a["value"]["stringValue"] for a in p["attributes"]}, p["asDouble"])
            for b in bodies for rm in b["resourceMetrics"] for sm in rm["scopeMetrics"]
            for m in sm["metrics"] for p in m["gauge"]["dataPoints"]]
