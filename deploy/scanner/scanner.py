"""evalskills static scanner (#65): runs inside the digest-pinned SkillEvaluator Tier 1 image, with no network.

It takes one job from the read-only inbox, scans it with `skillevaluator tier1 --no-llm`, writes the report files
to the outbox, and exits. Compose restarts it (`restart: always`), so every scan starts in a fresh container with
an empty /tmp, and nothing a crafted skill does survives into the next scan. It holds no secrets.

Protocol, shared with pipeline/evaluate.py:
  inbox/<job>/job.json, inbox/<job>/skill/<name>/   written by the app, then renamed into place (atomic)
  outbox/<job>/                                     created by the scanner to claim the job (mkdir is atomic)
  outbox/<job>/report/                              the evaluator's report files
  outbox/<job>/result.json                          exit code, duration, log tail, evaluator version, image
  outbox/<job>/done                                 written last; the app reads nothing before it exists
  outbox/.identity.json                             evaluator version and image, written at start
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

INBOX = Path(os.environ.get("SCANNER_IN", "/scan/in"))
OUTBOX = Path(os.environ.get("SCANNER_OUT", "/scan/out"))
IMAGE = os.environ.get("SCANNER_IMAGE", "")
TIMEOUT = int(os.environ.get("TIER1_TIMEOUT", "600"))
POLL_S = float(os.environ.get("SCANNER_POLL_S", "2"))
EVALUATOR = os.environ.get("SKILLEVALUATOR_BIN", "skillevaluator")
LOG_TAIL = 20_000
# Hard stop: if a scan outlives its cap by this much (a grandchild holding a pipe, say), PID 1 exits and the container
# dies with everything in it. It is shorter than the margin the app waits (60 s) before calling the scan timed out.
WATCHDOG_MARGIN_S = 30
# Docker doubles the restart delay (up to a minute) for a container that exits within 10 s of starting, which a quick
# scan does. The scanner waits this long before claiming a job, which keeps the delay at its 100 ms floor and leaves
# no idle time after a scan.
MIN_LIFE_S = float(os.environ.get("SCANNER_MIN_LIFE_S", "10.5"))


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def version() -> str | None:
    try:
        out = subprocess.run([EVALUATOR, "--version"], capture_output=True, text=True, timeout=60).stdout.split()
        return out[-1] if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def claim() -> Path | None:
    """The oldest ready job nobody has claimed, now claimed by us."""
    jobs = [d for d in INBOX.iterdir() if not d.name.startswith(".") and (d / "job.json").is_file()]
    for job in sorted(jobs, key=lambda d: d.stat().st_mtime):
        try:
            (OUTBOX / job.name).mkdir()
        except FileExistsError:
            continue
        return job
    return None


def kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_capped(cmd: list[str], cwd: Path) -> tuple[int, str]:
    """Run cmd in its own process group. On timeout the whole group is killed, so a grandchild (SkillSpector,
    gitleaks) can't keep the pipes open and hang us; the watchdog covers one that left the group."""
    p = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd, start_new_session=True
    )
    try:
        out, err = p.communicate(timeout=TIMEOUT)
        kill_group(p.pid)  # nothing it started outlives the scan
        return p.returncode, out + "\n--- stderr ---\n" + err
    except subprocess.TimeoutExpired:
        kill_group(p.pid)
        try:
            p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass  # something outside the group still holds a pipe; we report and exit, which ends it
        return -9, f"TIMEOUT: scan timed out after {TIMEOUT}s"


def end_stragglers() -> None:
    """As the container's PID 1, kill every other process in it, including one that left the scan's process group
    with setsid. Elsewhere (tests, a dev machine) this does nothing: kill(-1) there would reach the user's other
    processes."""
    if os.getpid() == 1:
        try:
            os.kill(-1, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def start_watchdog(seconds: float) -> threading.Timer:
    """Exit the process, hard, after seconds. As the container's PID 1, that ends every process in it."""
    t = threading.Timer(seconds, lambda: os._exit(70))
    t.daemon = True
    t.start()
    return t


def scan(job: Path, ver: str | None) -> None:
    out = OUTBOX / job.name
    spec = json.loads((job / "job.json").read_text())
    name = Path(spec["name"]).name  # a bare folder name, never a path
    work = Path(tempfile.mkdtemp(prefix="scan-"))
    skill = work / name
    shutil.copytree(job / "skill" / name, skill, symlinks=True)
    reports = work / "reports"
    cmd = [EVALUATOR, "tier1", str(skill), "--no-llm", "-r", "json", "-r", "html", "-r", "markdown", "-o", str(reports)]
    t0 = time.monotonic()
    code, log = run_capped(cmd, work)
    end_stragglers()  # before the report is copied, so nothing can change it or see the next job
    secs = round(time.monotonic() - t0, 1)
    if reports.is_dir():
        shutil.copytree(reports, out / "report", symlinks=True)  # links stay links; the app reads regular files only
    result = {"exit_code": code, "duration_s": secs, "log": log[-LOG_TAIL:], "version": ver, "image": IMAGE}
    write_atomic(out / "result.json", json.dumps(result))
    write_atomic(out / "done", "")
    shutil.rmtree(work, ignore_errors=True)


def serve(once: bool = True, max_wait_s: float | None = None, not_before: float = 0.0) -> bool:
    """Wait for a job and scan it. Returns False if max_wait_s passed with no job. No job is claimed before the
    monotonic time not_before, so the container's minimum life is spent idle, never after a scan."""
    Path(os.environ.get("HOME", "/tmp/evaluator-home")).mkdir(parents=True, exist_ok=True)
    ver = version()
    write_atomic(OUTBOX / ".identity.json", json.dumps({"version": ver, "image": IMAGE}))
    start = time.monotonic()
    time.sleep(max(0.0, not_before - start))
    while True:
        job = claim()
        if job:
            start_watchdog(TIMEOUT + WATCHDOG_MARGIN_S)
            try:
                scan(job, ver)
            except Exception as e:  # noqa: BLE001 -- a bad job is reported, never left claimed and silent
                result = {"exit_code": -1, "duration_s": 0, "log": f"scanner error: {type(e).__name__}", "version": ver}
                write_atomic(OUTBOX / job.name / "result.json", json.dumps({**result, "image": IMAGE}))
                write_atomic(OUTBOX / job.name / "done", "")
            if once:
                return True
        elif max_wait_s is not None and time.monotonic() - start > max_wait_s:
            return False
        else:
            time.sleep(POLL_S)


if __name__ == "__main__":
    serve(once=True, not_before=time.monotonic() + MIN_LIFE_S)
    # Exit right after `done` (the minimum life was spent before the claim). Compose restarts the container, so the
    # next scan gets a fresh one.
    sys.exit(0)
