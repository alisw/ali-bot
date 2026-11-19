#!/bin/bash
# runner.sh catch-up: a flow that hits PASS_TIMEOUT runs again while the runner
# would otherwise sleep, is published after that run, and the other flows wait
# for the next pass. Fake embedder (slow for the first run), file:// store, a
# blobless clone of alisw/ali-bot limited by date.
#   catchup_test.sh <venv> <workdir>
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
venv=$1 work=$2
mkdir -p "$work"
log=$work/embed.log out=$work/runner.log
: > "$log"; : > "$out"
echo 30 > "$log.delay"                       # every request waits 30 s: the first run times out
"$venv/bin/python" "$here/fake_embedder.py" 18779 "$log" & fp=$!
env COCO_ROOT="$work/root" VENV="$venv" SKIP_VENV=1 RESTORE=skip \
    STORE_URL="file://$work/bucket" FLOWS=commits SRC_REPOS= \
    OPENAI_API_BASE=http://127.0.0.1:18779/v1 OPENAI_API_KEY=fake \
    PASS_TIMEOUT=20s PASS_KILL_GRACE=10s PASS_SLEEP=150 CATCHUP_MIN_S=10 PROBE_INTERVAL=5 \
    COMMITS_REPOS=alisw/ali-bot:master COMMITS_SINCE=2026-09-01 DIFF_EMBED_SINCE=2026-09-01 \
    bash "$here/../runner.sh" 2> "$out" & rp=$!
trap 'kill $fp $rp 2>/dev/null || true' EXIT

wait_for() {  # wait_for PATTERN SECONDS
  local t=0
  until grep -q "$1" "$out"; do
    sleep 1; t=$((t + 1))
    [ "$t" -lt "$2" ] || { echo "FAIL: no '$1' after $2 s"; grep '^runner:' "$out"; exit 1; }
  done
}
wait_for "commits: hit PASS_TIMEOUT" 300
rm -f "$log.delay"                           # the embedder is fast again
wait_for "commits: cocoindex update, catching up" 60
wait_for "commits: done" 120
kill -TERM $rp; wait $rp || true

fail=0
check() { if eval "$1"; then echo "ok   $2"; else echo "FAIL $2"; fail=1; fi; }
r() { grep -c "$1" "$out" || true; }
check '[ "$(r "commits: hit PASS_TIMEOUT")" = 1 ]' "the first run timed out once"
check '[ "$(r "catching up (timeout 20s)")" = 1 ]' "one catch-up run, limited to PASS_TIMEOUT"
check '[ "$(r "commits: done")" = 1 ]' "the catch-up run completed"
check '[ "$(r "published run")" -ge 2 ]' "published after the pass and after the catch-up"
check '[ "$(r "pass order")" = 1 ]' "no new pass started during the catch-up"
check '[ -s "$work/root/flows/commits/commits.db" ]' "commits.db written"
grep '^runner:' "$out"
exit $fail
