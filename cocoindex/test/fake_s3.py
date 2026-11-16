"""Stand-in for s3.cern.ch (through the security-proxy), for offline tests of
ci_failures_flow.py only. Serves one bucket read-only from a directory tree.

    python3 fake_s3.py PORT ROOT LOG TOKEN [PAGE_SIZE]

ROOT/<bucket>/<key> is object <key> of <bucket>. Behaves like S3 where the flow
depends on it: ListObjectsV2 (list-type=2, max-keys capped at PAGE_SIZE so the
test sees several pages, continuation tokens), GET with Range (bytes=a-b and
bytes=-n, answering 206), ETag = md5 of the content, LastModified = the file's
mtime. A request without "Credential=<TOKEN>/" in its Authorization is a 403,
like the proxy with a stale gate token. Other methods are 405: the flow must
never write.

Every request is appended to LOG as one JSON line {op, key, bytes}, op one of
list | get | range, so a test can assert "no downloads".
"""
import hashlib
import http.server
import json
import os
import sys
import threading
import time
import urllib.parse
from xml.sax.saxutils import escape

PORT, ROOT, LOG, TOKEN = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
PAGE = int(sys.argv[5]) if len(sys.argv) > 5 else 1000
LOCK = threading.Lock()


def _objects(bucket):
    base = os.path.join(ROOT, bucket)
    out = []
    for d, _, files in os.walk(base):
        for f in files:
            p = os.path.join(d, f)
            out.append(os.path.relpath(p, base).replace(os.sep, "/"))
    return sorted(out)


def _meta(path):
    data = open(path, "rb").read()
    st = os.stat(path)
    lm = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(st.st_mtime))
    return data, hashlib.md5(data).hexdigest(), lm


class H(http.server.BaseHTTPRequestHandler):
    def _log(self, op, key, n):
        with LOCK, open(LOG, "a") as f:
            f.write(json.dumps({"op": op, "key": key, "bytes": n}) + "\n")

    def _send(self, code, body=b"", headers=()):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if f"Credential={TOKEN}/" not in self.headers.get("Authorization", ""):
            return self._send(403, b"<Error><Code>AccessDenied</Code></Error>")
        u = urllib.parse.urlsplit(self.path)
        bucket, _, key = u.path.lstrip("/").partition("/")
        key = urllib.parse.unquote(key)
        q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
        if not key:
            if q.get("list-type") != "2":
                return self._send(400, b"<Error><Code>OnlyListV2</Code></Error>")
            keys = _objects(bucket)
            start = int(q.get("continuation-token") or 0)
            n = min(int(q.get("max-keys", 1000)), PAGE)
            page = keys[start:start + n]
            parts = ['<?xml version="1.0" encoding="UTF-8"?>',
                     '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
                     f"<Name>{bucket}</Name><KeyCount>{len(page)}</KeyCount>",
                     f"<IsTruncated>{'true' if start + n < len(keys) else 'false'}</IsTruncated>"]
            if start + n < len(keys):
                parts.append(f"<NextContinuationToken>{start + n}</NextContinuationToken>")
            for k in page:
                _, etag, lm = _meta(os.path.join(ROOT, bucket, k))
                size = os.path.getsize(os.path.join(ROOT, bucket, k))
                parts.append(f"<Contents><Key>{escape(k)}</Key><LastModified>{lm}</LastModified>"
                             f"<ETag>&quot;{etag}&quot;</ETag><Size>{size}</Size></Contents>")
            parts.append("</ListBucketResult>")
            self._log("list", None, 0)
            return self._send(200, "".join(parts).encode(), [("Content-Type", "application/xml")])
        path = os.path.join(ROOT, bucket, key)
        if not os.path.isfile(path):
            return self._send(404, b"<Error><Code>NoSuchKey</Code></Error>")
        data, etag, lm = _meta(path)
        rng = self.headers.get("Range")
        hdrs = [("ETag", f'"{etag}"'), ("Last-Modified", lm), ("Content-Type", "text/plain")]
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            lo, hi = (max(0, len(data) - int(b)), len(data) - 1) if a == "" else (int(a), min(int(b or len(data) - 1), len(data) - 1))
            part = data[lo:hi + 1]
            self._log("range", key, len(part))
            return self._send(206, part, hdrs + [("Content-Range", f"bytes {lo}-{hi}/{len(data)}")])
        self._log("get", key, len(data))
        return self._send(200, data, hdrs)

    def _refuse(self):
        self._send(405, b"<Error><Code>MethodNotAllowed</Code></Error>")

    do_PUT = do_POST = do_DELETE = _refuse

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
