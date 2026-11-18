"""Stand-in for aigw's /v1/embeddings, for offline tests only.

Returns a deterministic 768-d unit vector per input (hash-seeded), so a
vector query against the result is meaningful-ish (identical text -> identical
vector) but semantically worthless. Appends one line per request to LOG with
the number of inputs, so a test can assert "zero embedding calls".

    python3 fake_embedder.py PORT LOG [TEXTLOG]

With TEXTLOG, every input text is also appended to it as one JSON line, so a
test can tell which flow's texts were embedded.

The embeddinggemma task prefixes (embedtext.py) are dropped before hashing: to
the real model they are an instruction, not content, so a document and a query
with the same text land close together. Here they land on the same vector, and
searching for a stored text still finds it at distance 0. TEXTLOG keeps the
texts as sent, prefixes included.
"""
import hashlib, http.server, json, os, sys, time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
import embedtext  # noqa: E402

PORT, LOG = int(sys.argv[1]), sys.argv[2]
TEXTLOG = sys.argv[3] if len(sys.argv) > 3 else None
DELAY = LOG + ".delay"   # if present: seconds to wait before answering (a slow aigw)


#: FAKE_EMBED_SALT: another, incompatible model -- the same text gets another
#: vector (embedroute.py must refuse such a server).
SALT = os.environ.get("FAKE_EMBED_SALT", "")


def vec(text: str) -> list[float]:
    for prefix in (embedtext.DOC_PREFIX, embedtext.QUERY_PREFIX):
        text = text.removeprefix(prefix)
    seed = int.from_bytes(hashlib.sha256((SALT + text).encode()).digest()[:8], "little")
    v = np.random.default_rng(seed).standard_normal(768).astype(np.float32)
    return (v / np.linalg.norm(v)).tolist()


class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        with open(LOG, "a") as f:
            f.write(f"{len(inputs)}\n")
        if TEXTLOG:
            with open(TEXTLOG, "a") as f:
                f.writelines(json.dumps(t) + "\n" for t in inputs)
        if os.path.exists(DELAY):
            time.sleep(float(open(DELAY).read()))
        out = {"object": "list", "model": body.get("model"),
               "data": [{"object": "embedding", "index": i, "embedding": vec(t)}
                        for i, t in enumerate(inputs)],
               # One "token" per word: plausible, and distinguishable from
               # metrics.py's fallback estimate (characters / 4).
               "usage": {"prompt_tokens": sum(len(t.split()) for t in inputs),
                         "total_tokens": sum(len(t.split()) for t in inputs)}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
