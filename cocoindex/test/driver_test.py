"""driver.sh: runs runner.sh from the code a deploy ref names, and reloads it
when the ref changes. A local git origin and a stub runner.sh that reports what
it runs and exits 75 when the ref changes (as runner.sh's reload_if_requested).

    python test/driver_test.py
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DRIVER = HERE.parent / "driver.sh"
failures = []


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


STUB = """#!/bin/bash
# stub runner: says which code runs, reloads (75) when the ref changes, TERM ends it
trap 'echo "stop $VERSION" >> "$LOG"; exit 0' TERM
echo "run $VERSION ref=${DEPLOYED_REF-unset} code=$CODE_DIR" >> "$LOG"
while :; do
  now=$(sed -n 's/^DEPLOY_REF=//p' "$DEPLOY_ENV" 2>/dev/null | tail -1)
  [ "$now" != "$DEPLOYED_REF" ] && exit 75
  sleep 0.1
done
"""


def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo, version, branch="master"):
    (repo / "cocoindex").mkdir(exist_ok=True)
    (repo / "cocoindex" / "runner.sh").write_text(STUB.replace("$VERSION", version))
    sh("git", "-C", str(repo), "add", "-A")
    sh("git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", version)
    return sh("git", "-C", str(repo), "rev-parse", "HEAD")


work = Path(tempfile.mkdtemp(prefix="driver-test-"))
try:
    src = work / "src"
    src.mkdir()
    sh("git", "init", "-q", "-b", "master", str(src))
    sha_a = commit(src, "A")
    sha_b = commit(src, "B")
    sh("git", "-C", str(src), "checkout", "-q", "-b", "other")
    sha_c = commit(src, "C")
    sh("git", "-C", str(src), "checkout", "-q", "master")
    origin = work / "origin.git"
    sh("git", "clone", "-q", "--bare", str(src), str(origin))

    shipped = work / "local"
    shipped.mkdir()
    (shipped / "runner.sh").write_text(STUB.replace("$VERSION", "shipped"))
    deploy_env, log = shipped / "deploy.env", work / "runs.log"
    deploy_env.write_text("")
    log.write_text("")
    coco = work / "coco"
    coco.mkdir()
    env = dict(os.environ, COCO_ROOT=str(coco), SHIPPED=str(shipped), DEPLOY_ENV=str(deploy_env),
               DEPLOY_REPO=f"file://{origin}", LOG=str(log))
    drv = subprocess.Popen(["bash", str(DRIVER)], env=env, stderr=open(work / "driver.log", "w"))

    def runs():
        return [l for l in log.read_text().splitlines() if l.startswith("run ")]

    def wait_for(n, timeout=30):
        t = time.time() + timeout
        while time.time() < t and len(runs()) < n:
            time.sleep(0.1)
        return runs()[-1] if len(runs()) >= n else None

    def deploy(ref):
        deploy_env.write_text(f"DEPLOY_REF={ref}\n" if ref else "")

    r = wait_for(1)
    check(r and r.startswith("run shipped ref= ") and r.endswith(f"code={shipped}"),
          f"no deploy ref: the jobspec's code runs ({r})")
    deploy(sha_a)
    r = wait_for(2)
    check(r and r.startswith(f"run A ref={sha_a}") and r.endswith(f"code={coco}/ali-bot/cocoindex"),
          f"a sha on master: that commit runs, from the checkout on the sticky disk ({r})")
    deploy("master")
    r = wait_for(3)
    check(r and r.startswith("run B ref=master"), f"a branch name: its head runs ({r})")
    deploy(sha_c)
    r = wait_for(4)
    check(r and r.startswith(f"run B ref={sha_c}"),
          f"a commit not on master is refused: the current code (B) keeps running ({r})")
    check("is not on master; refusing" in (work / "driver.log").read_text(), "and the refusal is logged")
    deploy("no-such-branch")
    r = wait_for(5)
    check(r and r.startswith("run B ref=no-such-branch"), f"an unknown ref: B keeps running ({r})")
    deploy("--upload-pack=evil")
    r = wait_for(6)
    time.sleep(1.5)
    check(len(runs()) == 6 and r and r.startswith("run B ref=--upload-pack=evil"),
          f"a malformed ref is ignored: B keeps running, and is not reloaded over and over ({runs()[-1:]})")
    deploy(sha_b)
    r = wait_for(7)
    check(r and r.startswith(f"run B ref={sha_b}"), f"back to a sha on master ({r})")
    drv.send_signal(signal.SIGTERM)
    rc = drv.wait(timeout=10)
    check(rc == 0 and log.read_text().splitlines()[-1] == "stop B",
          f"TERM: the runner is stopped and the driver exits 0 (rc={rc})")
finally:
    if 'drv' in dir() and drv.poll() is None:
        drv.kill()
    shutil.rmtree(work, ignore_errors=True)

print(f"\n{len(failures)} failure(s)")
sys.exit(1 if failures else 0)
