#!/bin/bash
# Local end-to-end check of the snapshot/restore protocol: file:// store, fake
# embedder, real (blobless) GitHub clone of alisw/ali-bot limited by date.
#   e2e.sh <venv> <workdir> <host-label>
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
venv=$1 work=$2 host=$3
: > "$work/embed-$host.log"
"$venv/bin/python" "$here/fake_embedder.py" 18778 "$work/embed-$host.log" & fp=$!
trap 'kill $fp' EXIT
sleep 1
env COCO_ROOT="$work/$host" VENV="$venv" SKIP_VENV=1 ONCE=1 \
    STORE_URL="file://$work/bucket" \
    OPENAI_API_BASE=http://127.0.0.1:18778/v1 OPENAI_API_KEY=fake \
    COMMITS_REPOS=alisw/ali-bot:master COMMITS_SINCE=2026-06-01 DIFF_EMBED_SINCE=2026-09-01 \
    bash "$here/../runner.sh"
echo "embedding requests on $host: $(wc -l < "$work/embed-$host.log") (texts: $(awk '{s+=$1} END {print s+0}' "$work/embed-$host.log"))"
