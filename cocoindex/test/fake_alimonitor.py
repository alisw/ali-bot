"""Stand-in for alimonitor's public Hyperloop test folders, for offline tests.

    python3 fake_alimonitor.py PORT ROOT LOG

Serves ROOT at / like alimonitor's Tomcat: a directory as an HTML listing (one
<tr> per entry: the name in <a><tt>, the size, the modification time in HTTP
date form, taken from the file system, so a test can age an entry with
os.utime), a file as itself, gzipped when the client accepts it. Appends one
line per request to LOG: "<path> <status>".
"""
import email.utils
import gzip
import http.server
import os
import sys
from pathlib import Path

PORT, ROOT, LOG = int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]


def listing(d: Path, url: str) -> bytes:
    rows = []
    for p in sorted(d.iterdir()):
        name = p.name + ("/" if p.is_dir() else "")
        size = "&nbsp;" if p.is_dir() else f"{p.stat().st_size / 1024:.1f} kb"
        mod = email.utils.formatdate(p.stat().st_mtime, usegmt=True)
        rows.append(f'<tr>\n<td align="left">&nbsp;&nbsp;\n<a href="{url}{name}/"><tt>{name}</tt></a></td>\n'
                    f'<td align="right"><tt>{size}</tt></td>\n<td align="right"><tt>{mod}</tt></td>\n</tr>')
    return (f"<html><head><title>Directory Listing For [{url}]</title></head><body><table>"
            + "\n".join(rows) + "</table></body></html>").encode()


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        rel = self.path.split("?", 1)[0].lstrip("/")
        p = (ROOT / rel).resolve()
        if ROOT.resolve() not in (p, *p.parents) or not p.exists():
            return self.reply(404, b"not found", "text/plain")
        if p.is_dir():
            return self.reply(200, listing(p, "/" + rel), "text/html;charset=UTF-8")
        return self.reply(200, p.read_bytes(), "application/octet-stream")

    def do_HEAD(self):
        rel = self.path.split("?", 1)[0].lstrip("/")
        p = (ROOT / rel).resolve()
        ok = ROOT.resolve() in (p, *p.parents) and p.exists()
        with open(LOG, "a") as f:
            f.write(f"HEAD {self.path} {200 if ok else 404}\n")
        self.send_response(200 if ok else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def reply(self, status, body, ctype):
        with open(LOG, "a") as f:
            f.write(f"{self.path} {status}\n")
        gz = "gzip" in (self.headers.get("Accept-Encoding") or "") and status == 200
        if gz:
            body = gzip.compress(body)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
