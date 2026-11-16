"""Stand-in for GitHub's GraphQL endpoint, for offline tests of prs_flow.py only.

Serves fixture PRs to the five fixed queries prs_flow.py sends, dispatched on
operationName (PRList, PRNodes, PRAssoc, PRMore_<connection>); it does not
parse GraphQL. Behaves like GitHub where the flow depends on it: newest-updated
first, keyset cursors, nested connections cut at $n with pageInfo, nodes(ids:)
with null + NOT_FOUND for unknown ids, rateLimit on every reply.

    python3 fake_github.py PORT LOG TOKEN

The test drives it over HTTP:
    POST /control  {"repos": {"owner/name": [PR, ...]}}   replace the fixtures
    POST /control  {"inject": {"http_403": 1, "graphql_rate_limited": 1, "low_remaining": 1}}
                   the next N replies are a 403 with x-ratelimit-remaining: 0, a
                   200 with a RATE_LIMITED error, or a normal reply whose
                   rateLimit.remaining is below any sane reserve (reset in 1 s).
Every GraphQL request is appended to LOG as one JSON line {op, variables, status}.

For ci_failures_flow.py it also serves (fixtures set through /control):
    {"statuses": {"owner/name@sha": [{context, state, created_at, description}, ...]}}
        GET /repos/<owner>/<name>/commits/<sha>/statuses (REST, newest first,
        per_page/page honoured); logged as op "REST:statuses"
    {"files": {"owner/name#pr": {"head": sha, "files": [path, ...]}}}
        GraphQL operation CIFFiles: aliases r0.. with variables o<i>, n<i>, p<i>
"""
import base64
import copy
import http.server
import json
import sys
import threading
import time

PORT, LOG, TOKEN = int(sys.argv[1]), sys.argv[2], sys.argv[3]
STATE = {"repos": {}, "inject": {}, "statuses": {}, "files": {}}
LOCK = threading.Lock()
THREAD_COMMENTS = 20


def _cursor(pr):
    return base64.b64encode(json.dumps([pr["updatedAt"], pr["number"]]).encode()).decode()


def _cut(conn, n, after=None):
    nodes = conn["nodes"]
    start = int(after) if after else 0
    page = nodes[start:start + n]
    return {"totalCount": len(nodes), "nodes": page,
            "pageInfo": {"hasNextPage": start + n < len(nodes), "endCursor": str(start + len(page))}}


def _thread(t):
    t = copy.deepcopy(t)
    c = t["comments"]["nodes"]
    t["comments"] = {"totalCount": len(c), "nodes": c[:THREAD_COMMENTS]}
    return t


def _shape(pr, n, nt):
    """A PR as GitHub returns it under `...PR` with first: n (threads: nt) nested."""
    pr = copy.deepcopy(pr)
    pr["comments"] = _cut(pr["comments"], n)
    pr["reviews"] = _cut(pr["reviews"], n)
    threads = {"nodes": [_thread(t) for t in pr["reviewThreads"]["nodes"]]}
    pr["reviewThreads"] = _cut(threads, nt)
    return pr


def _all():
    return {pr["id"]: pr for prs in STATE["repos"].values() for pr in prs}


def answer(op, v):
    errors = []
    if op == "PRList":
        prs = sorted(STATE["repos"].get(f"{v['owner']}/{v['name']}", []),
                     key=lambda p: (p["updatedAt"], p["number"]), reverse=True)
        if v.get("after"):
            key = tuple(json.loads(base64.b64decode(v["after"])))
            prs = [p for p in prs if (p["updatedAt"], p["number"]) < key]
        page = prs[:v["first"]]
        data = {"repository": {"pullRequests": {
            "pageInfo": {"hasNextPage": len(prs) > len(page),
                         "endCursor": _cursor(page[-1]) if page else None},
            "nodes": [_shape(p, v["n"], v["nt"]) for p in page]}}}
    elif op in ("PRNodes", "PRAssoc"):
        by_id, nodes = _all(), []
        for i, pid in enumerate(v["ids"]):
            p = by_id.get(pid)
            if p is None:
                nodes.append(None)
                errors.append({"type": "NOT_FOUND", "path": ["nodes", i], "message": f"no node {pid}"})
            elif op == "PRNodes":
                nodes.append(_shape(p, v["n"], v["nt"]))
            else:
                nodes.append({k: p[k] for k in ("id", "updatedAt", "authorAssociation", "author")})
        data = {"nodes": nodes}
    elif op == "CIFFiles":
        data, i = {}, 0
        while f"o{i}" in v:
            f = STATE["files"].get(f"{v[f'o{i}']}/{v[f'n{i}']}#{v[f'p{i}']}")
            if f is None:
                data[f"r{i}"] = {"pullRequest": None}
                errors.append({"type": "NOT_FOUND", "path": [f"r{i}", "pullRequest"], "message": "no PR"})
            else:
                data[f"r{i}"] = {"pullRequest": {"headRefOid": f["head"], "files": {
                    "totalCount": len(f["files"]), "nodes": [{"path": x} for x in f["files"][:100]]}}}
            i += 1
    elif op.startswith("PRMore_"):
        conn = op[len("PRMore_"):]
        p = copy.deepcopy(_all()[v["id"]])
        if conn == "reviewThreads":
            p[conn] = {"nodes": [_thread(t) for t in p[conn]["nodes"]]}
        data = {"node": {conn: _cut(p[conn], v["n"], v.get("after"))}}
    else:
        return {"errors": [{"message": f"fake_github: unknown operation {op}"}]}
    remaining = 4000
    if STATE["inject"].get("low_remaining"):
        STATE["inject"]["low_remaining"] -= 1
        remaining = 3
    data["rateLimit"] = {"cost": 1, "remaining": remaining,
                         "resetAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 2))}
    out = {"data": data}
    if errors:
        out["errors"] = errors
    return out


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, code, obj, headers=()):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, val in headers:
            self.send_header(k, val)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with LOCK:
            if self.path == "/control":
                if "repos" in req:
                    STATE["repos"] = req["repos"]
                for k in ("statuses", "files"):
                    if k in req:
                        STATE[k] = req[k]
                STATE["inject"].update(req.get("inject", {}))
                return self._send(200, {"ok": True})
            op, v = req.get("operationName"), req.get("variables") or {}
            status, inj = 200, STATE["inject"]
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                status, reply, hdrs = 401, {"message": "Bad credentials"}, ()
            elif inj.get("http_403"):
                inj["http_403"] -= 1
                status, reply = 403, {"message": "API rate limit exceeded"}
                hdrs = (("x-ratelimit-remaining", "0"), ("x-ratelimit-reset", str(int(time.time()) + 1)))
            elif inj.get("graphql_rate_limited"):
                inj["graphql_rate_limited"] -= 1
                reply = {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]}
                hdrs = (("x-ratelimit-remaining", "0"), ("x-ratelimit-reset", str(int(time.time()) + 1)))
            else:
                reply, hdrs = answer(op, v), ()
            with open(LOG, "a") as f:
                f.write(json.dumps({"op": op, "variables": v, "status": status,
                                    "rate_limited": "RATE_LIMITED" in json.dumps(reply.get("errors", ""))}) + "\n")
        self._send(status, reply, hdrs)

    def do_GET(self):
        import re
        import urllib.parse
        u = urllib.parse.urlsplit(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/commits/([0-9a-f]+)/statuses", u.path)
        with LOCK:
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                status, reply = 401, {"message": "Bad credentials"}
            elif not m:
                status, reply = 404, {"message": "Not Found"}
            else:
                allst = STATE["statuses"].get(f"{m.group(1)}@{m.group(2)}", [])
                per, page = int(q.get("per_page", 30)), int(q.get("page", 1))
                status, reply = 200, allst[(page - 1) * per:page * per]
            with open(LOG, "a") as f:
                f.write(json.dumps({"op": "REST:statuses", "variables": {"path": u.path, **q},
                                    "status": status, "rate_limited": False}) + "\n")
        self._send(status, reply)

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
