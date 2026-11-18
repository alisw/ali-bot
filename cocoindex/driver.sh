#!/bin/bash
# The cocoindex job's entry point: runs runner.sh from the code a Nomad
# variable names, and reloads it when the variable changes -- between flows or
# while the runner sleeps, never in the middle of a flow.
#
#   nomad-rw var put -force nomad/jobs/cocoindex ref=<sha|branch>   # deploy
#   nomad-rw var purge nomad/jobs/cocoindex                         # back to the jobspec's code
#
# The jobspec renders the variable into $DEPLOY_ENV ("DEPLOY_REF=<ref>", empty
# when the variable does not exist) with change_mode "noop". Without a ref the
# runner runs from $SHIPPED, the code the jobspec itself ships (as before this
# script existed). With one, from an ali-bot checkout on the sticky disk at that
# ref -- only if the commit is on $DEPLOY_BRANCH of $DEPLOY_REPO: nothing that
# is not on master runs with the job's credentials. A ref that cannot be
# fetched or is not on the branch is logged and the current code keeps running.
#
# runner.sh exits 75 at a safe point when $DEPLOY_ENV names another ref than
# the one it was started with ($DEPLOYED_REF); this script then reloads. The
# venv comes from the image: a change to requirements-linux.lock still needs a
# new image and a jobspec deploy, as does any change to the jobspec itself.
set -u

DEPLOY_ENV=${DEPLOY_ENV:-/local/deploy.env}
SHIPPED=${SHIPPED:-/local}
DEPLOY_REPO=${DEPLOY_REPO:-https://github.com/alisw/ali-bot}
DEPLOY_BRANCH=${DEPLOY_BRANCH:-master}
CHECKOUT=${CHECKOUT:-$COCO_ROOT/ali-bot}
RELOAD_RC=75

log() { echo "driver: $(date -u +%FT%TZ) $*" >&2; }

read_raw() { sed -n 's/^DEPLOY_REF=//p' "$DEPLOY_ENV" 2>/dev/null | tail -1; }

read_ref() {
  local r
  r=$(read_raw)
  # A ref is a sha, a branch or a tag: nothing a shell or git could misread.
  [[ -z "$r" || "$r" =~ ^[A-Za-z0-9._/-]+$ && "$r" != -* ]] || { log "ignoring malformed ref ${r@Q}"; r=; }
  echo "$r"
}

# Points the checkout at $1; prints the code dir on success.
checkout() {
  local ref=$1 sha
  if [ ! -d "$CHECKOUT/.git" ]; then
    rm -rf "$CHECKOUT.tmp"
    git clone -q --filter=blob:none --no-checkout "$DEPLOY_REPO" "$CHECKOUT.tmp" && mv "$CHECKOUT.tmp" "$CHECKOUT" ||
      { log "clone of $DEPLOY_REPO failed"; return 1; }
  fi
  find "$CHECKOUT/.git" -name '*.lock' -not -path '*/objects/*' -delete 2>/dev/null || true
  git -C "$CHECKOUT" fetch -q origin "+refs/heads/$DEPLOY_BRANCH:refs/remotes/origin/$DEPLOY_BRANCH" ||
    { log "fetch of $DEPLOY_BRANCH failed"; return 1; }
  # A sha on the branch is there now; a branch or tag name needs its own fetch.
  sha=$(git -C "$CHECKOUT" rev-parse -q --verify "$ref^{commit}" 2>/dev/null) ||
    { git -C "$CHECKOUT" fetch -q origin "$ref" 2>/dev/null &&
      sha=$(git -C "$CHECKOUT" rev-parse -q --verify "FETCH_HEAD^{commit}"); } ||
    { log "ref $ref not found in $DEPLOY_REPO"; return 1; }
  git -C "$CHECKOUT" merge-base --is-ancestor "$sha" "origin/$DEPLOY_BRANCH" ||
    { log "ref $ref ($sha) is not on $DEPLOY_BRANCH; refusing to run it"; return 1; }
  git -C "$CHECKOUT" checkout -q -f --detach "$sha" || { log "checkout of $sha failed"; return 1; }
  [ -f "$CHECKOUT/cocoindex/runner.sh" ] || { log "$sha has no cocoindex/runner.sh"; return 1; }
  log "code: $DEPLOY_REPO@$sha (ref $ref)"
  echo "$CHECKOUT/cocoindex"
}

child='' stopping=''
trap 'stopping=1; [ -n "$child" ] && kill -TERM "$child" 2>/dev/null' TERM INT

code=$SHIPPED running_ref=
while :; do
  # The runner reloads when the file no longer says what it said here, so it
  # is given the raw value: a malformed or refused ref then waits for the next
  # change of the variable instead of reloading over and over.
  raw=$(read_raw)
  ref=$(read_ref)
  if [ -n "$raw" ] && [ -z "$ref" ]; then
    log "keeping the current code (${running_ref:-the jobspec code}): malformed ref"
  elif [ -z "$ref" ]; then
    code=$SHIPPED running_ref=
    log "code: the jobspec's ($SHIPPED); no deploy ref set"
  elif dir=$(checkout "$ref"); then
    code=$dir running_ref=$ref
  else
    log "keeping the current code (${running_ref:-the jobspec code})"
  fi
  CODE_DIR=$code DEPLOYED_REF=$raw DEPLOY_ENV=$DEPLOY_ENV \
    PROXY_TOKEN=${PROXY_TOKEN:-$SHIPPED/proxy-token.py} bash "$code/runner.sh" &
  child=$!
  rc=0
  wait "$child" || rc=$?
  # A trapped signal interrupts `wait`; wait again for the real exit status.
  while kill -0 "$child" 2>/dev/null; do rc=0; wait "$child" || rc=$?; done
  child=
  [ -n "$stopping" ] && exit 0
  [ "$rc" = "$RELOAD_RC" ] || exit "$rc"
  log "reload requested: $(read_ref || true)"
done
