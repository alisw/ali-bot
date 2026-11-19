#!/bin/bash
# embedtext's adaptive limit on texts in flight, end to end: the commits flow
# against a fake embedder that embeds FAKE_EMBED_RATE texts/s one request after
# the other (a saturated GPU) with a short EMBED_TIMEOUT_S, once with the limit
# off and once on. With it on, fewer commits fail and more are indexed.
#   [RATE=texts/s] adaptive_test.sh <venv> <workdir>
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
venv=$1 work=$2
mkdir -p "$work"
declare -A failed indexed
for mode in 0 1; do
  root=$work/adaptive-$mode log=$work/embed-$mode.log out=$work/runner-$mode.log
  rm -rf "$root"; : > "$log"
  FAKE_EMBED_RATE=${RATE:-40} "$venv/bin/python" "$here/fake_embedder.py" 18780 "$log" & fp=$!
  sleep 1
  env COCO_ROOT="$root" VENV="$venv" SKIP_VENV=1 ONCE=1 RESTORE=skip CATCHUP=0 \
      STORE_URL="file://$work/bucket-$mode" FLOWS=commits SRC_REPOS= \
      OPENAI_API_BASE=http://127.0.0.1:18780/v1 OPENAI_API_KEY=fake \
      EMBED_ADAPTIVE=$mode EMBED_TIMEOUT_S=4 \
      COMMITS_REPOS=alisw/ali-bot:master COMMITS_SINCE=2026-03-01 DIFF_EMBED_SINCE=2026-03-01 \
      bash "$here/../runner.sh" > /dev/null 2> "$out" || true
  kill $fp 2>/dev/null || true; wait $fp 2>/dev/null || true
  failed[$mode]=$(grep -c "ERROR cocoindex: .*process_commit.* failed" "$out" || true)
  indexed[$mode]=$("$venv/bin/python" -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('select count(*) from commits').fetchone()[0])" "$root/flows/commits/commits.db" 2>/dev/null || echo 0)
  echo "EMBED_ADAPTIVE=$mode: failed commits ${failed[$mode]}, indexed ${indexed[$mode]}, timeouts $(grep -c 'DeadlineExceeded' "$out" || true), requests $(wc -l < "$log")"
  grep '^embedtext:' "$out" | head -8 || true
done
fail=0
check() { if eval "$1"; then echo "ok   $2"; else echo "FAIL $2"; fail=1; fi; }
check '[ "${failed[0]}" -gt 0 ]' "without the limit the slow server fails commits"
check '[ "${failed[1]}" -lt "${failed[0]}" ]' "with the limit fewer commits fail (${failed[1]} < ${failed[0]})"
check '[ "${indexed[1]}" -ge "${indexed[0]}" ]' "with the limit at least as many commits are indexed (${indexed[1]} >= ${indexed[0]})"
exit $fail
