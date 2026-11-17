#!/bin/bash
# cocoindex runner: one long-lived loop, like the CI workers' claim loop.
#
#   startup:  venv; for each flow with no local state+db, restore BOTH from the
#             run id latest.json names (s3snap.py restore)
#   pass:     fetch mirrors -> `cocoindex update` per flow, sequentially, each
#             under its own timeout -> snapshot + publish + prune (s3snap.py)
#             -> sleep PASS_SLEEP
#
# Nothing else writes COCO_ROOT, and the snapshot runs only between passes, so
# every LMDB environment and SQLite file is closed when it is copied.
#
# Credentials are resolved per pass from the security-proxy sidecar (the gate
# token rotates daily, so a value resolved at startup would go stale), and only
# ever into the environment, never onto disk and never into a cocoindex memo
# key. There is no `set -x` anywhere in this script for the same reason.
set -euo pipefail

: "${COCO_ROOT:?COCO_ROOT must point at the sticky ephemeral disk}"
CODE_DIR=${CODE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}
FLOWS=${FLOWS:-commits}                       # comma list: commits, prs, ci-failures
FLOW_ORDER=${FLOW_ORDER:-random}              # random: shuffled every pass; fixed: as listed
PASS_SLEEP=${PASS_SLEEP:-3600}                # seconds between passes
PROBE_INTERVAL=${PROBE_INTERVAL:-300}         # between passes, time one embedding per route this often
EMBED_PROBE_ROUTES=${EMBED_PROBE_ROUTES:-aigw} # comma list of proxy routes to probe
PASS_TIMEOUT=${PASS_TIMEOUT:-50m}             # per flow per pass; SIGINT, then KILL
PASS_KILL_GRACE=${PASS_KILL_GRACE:-2m}
SNAPSHOT_KEEP=${SNAPSHOT_KEEP:-12}            # run ids kept in the bucket
SNAPSHOT_MIN_INTERVAL=${SNAPSHOT_MIN_INTERVAL:-0}
MIRROR_FILTER=${MIRROR_FILTER-blob:none}      # empty = full mirrors
GIT_BASE_URL=${GIT_BASE_URL:-https://github.com}  # where mirrors clone from (tests: file://)
COCOINDEX_PIN=${COCOINDEX_PIN:-cocoindex[litellm,sqlite]==1.2.0}
VENV=${VENV:-$COCO_ROOT/venv}
SKIP_VENV=${SKIP_VENV:-0}                     # 1 = use $VENV as is (local tests)
PROXY_SOCK=${PROXY_SOCK:-}                    # empty = endpoints already in env (tests)
PROXY_TOKEN=${PROXY_TOKEN:-$CODE_DIR/proxy-token.py}
RESTORE=${RESTORE:-auto}                      # auto | skip (= accept re-embedding)
ONCE=${ONCE:-0}                               # 1 = a single pass, no sleep (tests)
export COMMITS_REPOS=${COMMITS_REPOS:-AliceO2Group/AliceO2:dev,AliceO2Group/O2Physics:master,AliceO2Group/O2DPG:master,alisw/alidist:master,alisw/ali-bot:master}
# Where the public templates of runtime error messages come from (srctemplates.py);
# empty = none (ci-failures then publishes those errors by hash only).
SRC_REPOS=${SRC_REPOS-AliceO2Group/AliceO2:dev,AliceO2Group/O2Physics:master,AliceO2Group/O2DPG:master,AliceO2Group/QualityControl:master,root-project/root:master,FairRootGroup/FairMQ:dev,FairRootGroup/FairRoot:dev}
export SRCTEMPLATES="$COCO_ROOT/sources"

export COCOINDEX_DISABLE_USAGE_TRACKING=1
export GIT_TERMINAL_PROMPT=0
# litellm prefers OPENAI_BASE_URL over OPENAI_API_BASE; make sure ours wins.
unset OPENAI_BASE_URL

log() { echo "runner: $(date -u +%FT%TZ) $*" >&2; }

flow_file() {
  case $1 in
    commits)     echo commits_flow.py ;;
    prs)         echo prs_flow.py ;;
    ci-failures) echo ci_failures_flow.py ;;
    *) return 1 ;;
  esac
}
flow_db() {
  case $1 in
    commits)     echo commits.db ;;
    prs)         echo prs.db ;;
    ci-failures) echo ci-failures.db ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------- venv
ensure_venv() {
  [ "$SKIP_VENV" = 1 ] && return 0
  local pin
  pin="$COCOINDEX_PIN boto3 zstandard $(python3 -V 2>&1)"
  if [ "$(cat "$VENV/.pin" 2>/dev/null)" != "$pin" ]; then
    log "installing venv ($pin)"
    python3 -m venv --clear "$VENV"
    "$VENV/bin/pip" install -q --disable-pip-version-check "$COCOINDEX_PIN" boto3 zstandard
    echo "$pin" > "$VENV/.pin"
  fi
}

# ---------------------------------------------------------------- endpoints
# Exports OPENAI_API_BASE/OPENAI_API_KEY (aigw route), S3_ENDPOINT_URL/
# AWS_ACCESS_KEY_ID (s3 route: snapshots, and the build logs the ci-failures
# flow reads) and, for the prs and ci-failures flows, GITHUB_GRAPHQL_URL/
# GITHUB_TOKEN (github route). The access key IS the gate token; the proxy
# re-signs with the real keys, so the secret is a dummy. Likewise GITHUB_TOKEN
# is the gate token, swapped for the PAT upstream; prs_flow.py asks again
# (PROXY_SOCK/PROXY_TOKEN) if it gets a 401 after the daily rotation.
resolve_endpoints() {
  [ -z "$PROXY_SOCK" ] && return 0
  local host port
  ask() { python3 "$PROXY_TOKEN" "$PROXY_SOCK" "$1" "$2"; }
  port=$(ask aigw port) || return 1
  host=$(ask aigw host 2>/dev/null) || host=127.0.0.1
  OPENAI_API_BASE="http://$host:$port/aigw/v1"
  OPENAI_API_KEY=$(ask aigw token) || return 1
  S3_ENDPOINT_URL="http://$host:$port"
  AWS_ACCESS_KEY_ID=$(ask s3 token) || return 1
  AWS_SECRET_ACCESS_KEY=unused
  export OPENAI_API_BASE OPENAI_API_KEY S3_ENDPOINT_URL AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
  if [[ ",$FLOWS," == *,prs,* || ",$FLOWS," == *,ci-failures,* ]]; then
    GITHUB_GRAPHQL_URL="http://$host:$port/github/graphql"
    GITHUB_TOKEN=$(ask github token) || return 1
    export GITHUB_GRAPHQL_URL GITHUB_TOKEN PROXY_SOCK PROXY_TOKEN
  fi
  # Metrics (metrics.py) go to MONIT through the monit-otlp route, when there
  # is one: without it nothing is sent, and nothing else changes.
  if OTLP_WRITE_TOKEN=$(ask monit-otlp token 2>/dev/null); then
    OTLP_METRICS_URL="http://$host:$port/otlp/v1/metrics"
    export OTLP_METRICS_URL OTLP_WRITE_TOKEN
  else
    unset OTLP_METRICS_URL OTLP_WRITE_TOKEN
  fi
}

# One point to MONIT: metric NAME TAG=V ... -- FIELD=V ... (metrics.py push).
# Best effort: a metric never fails the pass.
metric() {
  [ -n "${OTLP_METRICS_URL:-}" ] || return 0
  "$VENV/bin/python" "$CODE_DIR/metrics.py" push "$@" >/dev/null 2>&1 || true
}

# Between passes: one short embedding per route in EMBED_PROBE_ROUTES, timed.
# The indexer's own requests say how fast the embedder was under its load; this
# says how fast it is at all, every PROBE_INTERVAL, whatever the indexer does.
probe_embedders() {
  [ -n "${OTLP_METRICS_URL:-}" ] && [ -n "$PROXY_SOCK" ] || return 0
  local r k port host
  port=$(ask aigw port 2>/dev/null) || return 0
  host=$(ask aigw host 2>/dev/null) || host=127.0.0.1
  for r in ${EMBED_PROBE_ROUTES//,/ }; do
    k=${r^^}; k=${k//-/_}
    export "PROBE_${k}_URL=http://$host:$port/$r/v1"
    export "PROBE_${k}_TOKEN=$(ask "$r" token 2>/dev/null)"
  done
  "$VENV/bin/python" "$CODE_DIR/metrics.py" probe ${EMBED_PROBE_ROUTES//,/ } >/dev/null 2>&1 || true
}

# The proxy and its bootstrap are prestart sidecars: Nomad starts this task once
# they have STARTED, not once the proxy has its secrets. Wait until the agent
# socket answers and the s3 route has its keys (it answers 503 until then),
# rather than exiting and being restarted a minute later.
PROXY_WAIT_S=${PROXY_WAIT_S:-300}
s3_ready() {
  local bucket=${STORE_URL#s3://}
  [[ "$STORE_URL" == s3://* ]] || return 0
  python3 - "$S3_ENDPOINT_URL/${bucket%%/*}?list-type=2&max-keys=1" "$AWS_ACCESS_KEY_ID" <<'PY'
import sys, urllib.error, urllib.request
url, token = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url, headers={
    "Authorization": f"AWS4-HMAC-SHA256 Credential={token}/00000000/default/s3/aws4_request, "
                     "SignedHeaders=host, Signature=unsigned",
    "x-amz-content-sha256": "UNSIGNED-PAYLOAD"})
try:
    urllib.request.urlopen(req, timeout=10).close()
except urllib.error.HTTPError as e:
    sys.exit(e.code == 503)      # anything but 503: the keys are there
except OSError:
    sys.exit(1)                  # proxy not listening yet
PY
}

wait_for_proxy() {
  [ -z "$PROXY_SOCK" ] && return 0
  local deadline=$(( $(date +%s) + PROXY_WAIT_S )) waited=0
  until resolve_endpoints 2>/dev/null && s3_ready; do
    if (( $(date +%s) >= deadline )); then
      log "the proxy is not ready after ${PROXY_WAIT_S}s"
      return 1
    fi
    waited=1
    sleep 5
  done
  [ "$waited" = 1 ] && log "proxy ready"
  return 0
}

# ---------------------------------------------------------------- mirrors
# Bare, branches only (a GitHub --mirror would also pull every refs/pull/*),
# blobless by default: blobs arrive on demand when the flow runs git show.
# prs_flow.py adds refs/pull/<n>/head only for the unmerged PRs whose diff it
# indexes; --prune leaves those alone, it only prunes the refspec's refs/heads/*.
fetch_mirrors() {
  local spec r m
  mkdir -p "$COCO_ROOT/mirrors"
  IFS=, read -r -a specs <<< "$COMMITS_REPOS"
  for spec in "${specs[@]}"; do
    r=${spec%%:*}
    m="$COCO_ROOT/mirrors/${r//\//__}.git"
    if [ ! -d "$m" ]; then
      log "cloning $r (filter: ${MIRROR_FILTER:-none})"
      rm -rf "$m.tmp"
      git clone -q --bare ${MIRROR_FILTER:+--filter="$MIRROR_FILTER"} "$GIT_BASE_URL/$r" "$m.tmp" ||
        { log "clone of $r failed"; continue; }
      git --git-dir "$m.tmp" config remote.origin.fetch '+refs/heads/*:refs/heads/*'
      mv "$m.tmp" "$m"
    fi
    # Leftovers of a git killed mid-write. Safe only because this loop and the
    # flow it runs afterwards are the sole users of the mirror.
    find "$m" -name '*.lock' -not -path '*/objects/*' -delete 2>/dev/null || true
    git --git-dir "$m" fetch -q --prune origin || log "fetch of $r failed; indexing the stale mirror"
    # Every lazy blob fetch leaves a small pack; fold them before they pile up.
    git --git-dir "$m" gc --auto -q || true
  done
}

# ---------------------------------------------------------------- sources
# Shallow, sparse working trees: C++, shell, Python and CMake files at the branch
# head, nothing else (ROOT's interpreter/ alone is larger than all the rest).
# A partial clone, so checkout fetches exactly those blobs, in one batch.
# srctemplates.py rebuilds its index only when a head moved.
SRC_SPARSE=('*.cxx' '*.cpp' '*.cc' '*.h' '*.hpp' '*.hxx' '*.C' '*.inl' '*.icc' 'CMakeLists.txt' '*.cmake' '*.sh' '*.py'
            '!/interpreter/**' '!/roottest/**' '!/tutorials/**' '!**/test_data/**')   # "dir/**": a bare "dir/" excludes nothing
fetch_sources() {
  local spec r b w names=()
  [ -n "$SRC_REPOS" ] || return 0
  mkdir -p "$SRCTEMPLATES"
  IFS=, read -r -a specs <<< "$SRC_REPOS"
  for spec in "${specs[@]}"; do
    r=${spec%%:*} b=${spec#*:}
    w="$SRCTEMPLATES/${r//\//__}"
    if [ ! -d "$w/.git" ]; then
      log "cloning sources of $r ($b)"
      rm -rf "$w.tmp"
      { git clone -q --depth 1 --filter=blob:none --no-checkout -b "$b" "$GIT_BASE_URL/$r" "$w.tmp" &&
        git -C "$w.tmp" sparse-checkout set --no-cone "${SRC_SPARSE[@]}" &&
        git -C "$w.tmp" checkout -q "$b" && mv "$w.tmp" "$w"; } 2>/dev/null ||
        { log "clone of the sources of $r failed"; rm -rf "$w.tmp"; continue; }
    else
      find "$w/.git" -name '*.lock' -not -path '*/objects/*' -delete 2>/dev/null || true
      { git -C "$w" fetch -q --depth 1 origin "$b" && git -C "$w" checkout -q -f --detach FETCH_HEAD; } 2>/dev/null ||
        log "fetch of the sources of $r failed; keeping the old tree"
    fi
    names+=("$r=$b")
  done
  "$VENV/bin/python" "$CODE_DIR/srctemplates.py" build "$SRCTEMPLATES" "${names[@]}" ||
    log "srctemplates build failed; publishing with the previous index (or none)"
}

# ---------------------------------------------------------------- one flow
child=""
stopping=0
on_term() {
  stopping=1
  log "termination requested"
  # timeout(1) forwards the signal to cocoindex, which stops at a component
  # boundary; LMDB and SQLite are crash-safe even if it does not get that far.
  [ -n "$child" ] && kill -INT "$child" 2>/dev/null || true
}
trap on_term TERM INT

run_flow() {
  local f=$1 d rc=0 var t0
  t0=$(date +%s)
  d="$COCO_ROOT/flows/$f"
  mkdir -p "$d"
  var=${f//-/_}
  export "FLOW_SHA_$var=$(sha256sum "$CODE_DIR/$(flow_file "$f")" | cut -d' ' -f1)"
  log "$f: cocoindex update (timeout $PASS_TIMEOUT)"
  COCOINDEX_DB="$d/state" FLOW_DB="$d/$(flow_db "$f")" COMMITS_MIRRORS="$COCO_ROOT/mirrors" \
    timeout --signal=INT --kill-after="$PASS_KILL_GRACE" "$PASS_TIMEOUT" \
    "$VENV/bin/cocoindex" update -f "$CODE_DIR/$(flow_file "$f")" &
  child=$!
  # A trapped signal interrupts `wait`; wait again for the real exit status.
  wait "$child" || rc=$?
  while kill -0 "$child" 2>/dev/null; do rc=0; wait "$child" || rc=$?; done
  child=""
  export "PASS_EXIT_$var=$rc"
  metric cocoindex_flow "flow=$f" "position=${2:-0}" -- "seconds=$(( $(date +%s) - t0 ))" \
    "timed_out=$(( rc == 124 ))" "exit_code=$rc"
  case $rc in
    0)   log "$f: done" ;;
    124) log "$f: hit PASS_TIMEOUT; memoized progress is kept, the next pass resumes" ;;
    *)   log "$f: exited $rc" ;;
  esac
}

snapshot() {
  local run_id
  run_id=$(date -u +%Y%m%dT%H%M%SZ)
  resolve_endpoints || { log "cannot resolve S3 endpoint; snapshot skipped"; return 0; }
  COCOINDEX_VERSION=$("$VENV/bin/python" -c 'import importlib.metadata as m; print(m.version("cocoindex"))') \
    "$VENV/bin/python" "$CODE_DIR/s3snap.py" publish --root "$COCO_ROOT" --flows "$FLOWS" \
      --run-id "$run_id" --keep "$SNAPSHOT_KEEP" --min-interval "$SNAPSHOT_MIN_INTERVAL" ||
    log "snapshot $run_id failed; local state is intact, the next pass retries"
}

# The flows of one pass, in the order they run. Shuffled every pass, so a flow
# that hits PASS_TIMEOUT (commits and prs, while the embedder is slow) does not
# always run before the others: each gets to go first, with the GitHub budget
# untouched and its data freshest when the pass publishes.
pass_order() {
  local -a a=(${FLOWS//,/ })
  local i j t
  if [ "$FLOW_ORDER" = random ]; then
    for (( i = ${#a[@]} - 1; i > 0; i-- )); do
      j=$(( RANDOM % (i + 1) )); t=${a[i]}; a[i]=${a[j]}; a[j]=$t
    done
  fi
  echo "${a[*]}"
}

# ---------------------------------------------------------------- main
mkdir -p "$COCO_ROOT/flows"
for f in ${FLOWS//,/ }; do
  flow_file "$f" > /dev/null || { log "unknown flow $f"; exit 2; }
done
ensure_venv

# Exits non-zero only if the proxy never becomes ready: Nomad's restart then.
wait_for_proxy || exit 1

if [ "$RESTORE" = skip ]; then
  log "RESTORE=skip: starting from whatever is on local disk (empty = full re-embed)"
else
  resolve_endpoints || { log "cannot resolve the S3 endpoint for restore"; exit 1; }
  # Non-zero here (unreadable latest.json, checksum mismatch, ...) ends the
  # task and Nomad retries it later: starting empty instead would re-embed
  # everything and then publish that over a good snapshot.
  "$VENV/bin/python" "$CODE_DIR/s3snap.py" restore --root "$COCO_ROOT" --flows "$FLOWS"
fi

while :; do
  started=$(date +%s)
  resolve_endpoints || log "cannot resolve proxy endpoints; flows will fail this pass"
  [[ ",$FLOWS," == *,commits,* || ( ",$FLOWS," == *,prs,* && ${PRS_DIFFS:-1} = 1 ) ]] && fetch_mirrors
  [[ ",$FLOWS," == *,ci-failures,* ]] && fetch_sources
  order=$(pass_order)
  log "pass order: $order"
  pos=0
  for f in $order; do
    [ "$stopping" = 1 ] && break
    resolve_endpoints || true      # a long previous flow may have outlived the token window
    pos=$((pos + 1))
    run_flow "$f" "$pos"
  done
  [ "$stopping" = 1 ] && { log "stopping before snapshot"; exit 0; }
  snapshot
  metric cocoindex_pass -- "seconds=$(( $(date +%s) - started ))"
  [ "$ONCE" = 1 ] && exit 0
  log "pass took $(( $(date +%s) - started ))s; sleeping ${PASS_SLEEP}s"
  until_next=$(( $(date +%s) + PASS_SLEEP ))
  while [ "$stopping" != 1 ] && [ "$(date +%s)" -lt "$until_next" ]; do
    resolve_endpoints >/dev/null 2>&1 || true
    probe_embedders
    nap=$(( until_next - $(date +%s) )); [ "$nap" -gt "$PROBE_INTERVAL" ] && nap=$PROBE_INTERVAL
    [ "$nap" -gt 0 ] || break
    sleep "$nap" & child=$!
    wait "$child" || true
    child=""
  done
  [ "$stopping" = 1 ] && exit 0
done
