"""The internal live pilot (#121): run pilot/skills.tsv through Tier 3, one run at a time, privately.

Each run gets its own gateway token, issued and copied without being printed, and revoked when the run ends. The
gateway's counts for that run (requests, tokens) go into its record. Records go to reports/pilot/results.jsonl,
which is never committed and never reaches the public build (ADR-0007: the board shows no pilot result; E12 in
docs/EXPERIMENT_LOG.md publishes per-skill results).

Run on the VPS, as root from /opt/evalskills, after deploy/gvisor/ and deploy/live/ are set up. SkillEvaluator itself
runs as the trial user (tier3.TRIAL_USER), through the socket proxy:
  SKILLEVALUATOR_DIR=/opt/skillevaluator DOCKER_HOST=unix:///run/evalskills/docker.sock \\
  TIER3_GATEWAY_URL=http://evals-gateway:8443/v1 TIER3_AGENT_MODEL=nvidia/nemotron-3.5-super-vl-120b-a12b \\
  SKILL_EVAL_LLM_MODEL=nvidia/nemotron-3.5-super-vl-120b-a12b \\
  python3 -I pipeline/live_pilot.py [--only SLUG] [--smoke] [--dry-run]
  python3 -I pipeline/live_pilot.py --cleanup          # after a hard kill only; see Stopping below

--smoke is the fast check that comes before every full run (docs/CONVENTIONS.md, Live runs; #257): each selected skill
runs once, on its first case only, with both arms, so the whole path (agent, grader, record) is tested in minutes. Its
records say "smoke": true. They check the pipeline, and are never read or reported as a pilot result. A skill whose
own cases can't be cut to one (anything but a plain evals.json) is refused and recorded failed, never run in full.
--dry-run prints the plan, runs and trials, without starting anything.

Stopping (#259): Ctrl-C once lets the current run finish, then stops; the rest of the plan is not started. Ctrl-C
again, or SIGTERM, stops the current trial now: it is cleaned up like a timed-out one and recorded failed, "stopped by
the operator", with no score. After a hard kill (SIGKILL, a lost session, a reboot), --cleanup kills what the trial
user left running, removes the trial's containers, networks and images through the socket proxy, revokes every
gateway token still open and removes the run folders. It refuses while a pilot holds the lock.
"""

import datetime as dt
import fcntl
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate as ev  # noqa: E402
import tier3  # noqa: E402

SKILLS = ev.ROOT / "pilot" / "skills.tsv"  # not under catalog/: the server reads every TSV there
RECORDS = ev.REPORTS / "pilot" / "results.jsonl"
SMOKE_CASES = 1  # --smoke: the first case only (#257)
GATEWAY = os.environ.get("PILOT_GATEWAY_CONTAINER", "evalskills-gateway")
MAIN_DOCKER = os.environ.get("PILOT_MAIN_DOCKER_HOST", "unix:///var/run/docker.sock")  # where the gateway runs
GATEWAY_CONFIG = "/etc/evalskills/gateway.json"
GATEWAY_STATE = "/var/lib/evalskills-gateway"
EGRESS_PROXY = os.environ.get("PILOT_EGRESS_CONTAINER", "evalskills-egress-proxy")
EGRESS_CONFIG = "/etc/evalskills/egress-proxy.json"


def load_skills(path: Path = SKILLS) -> list[dict]:
    """repo, skill_path, category, task_set ('-' for the skill's own cases), repeats."""
    rows = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) != 5:
            raise ValueError(f"{path.name}:{n}: expected 5 tab-separated columns, got {len(cols)}")
        repo, skill_path, category, task_set, repeats = cols
        if not ev.REPO_RE.fullmatch(repo):
            raise ValueError(f"{path.name}:{n}: bad repo {repo!r}")
        if skill_path.startswith("/") or ".." in skill_path.split("/"):
            raise ValueError(f"{path.name}:{n}: bad skill path {skill_path!r}")
        rows.append(
            {
                "repo": repo,
                "skill_path": skill_path,
                "category": category,
                "task_set": None if task_set == "-" else task_set,
                "repeats": int(repeats),
            }
        )
    return rows


def slug(row: dict) -> str:
    return f"{row['repo']}/{row['skill_path']}".replace("/", "__").replace(".", "_")


def run_id_for(row: dict, repeat: int, now: dt.datetime, smoke: bool = False) -> str:
    """Unique per run: the gateway refuses a reused run ID, so a same-day retry needs the time, not just the date.
    A smoke run's ID starts "smoke-", so every progress line and record shows it isn't a pilot measurement."""
    return f"{'smoke' if smoke else 'pilot'}-{now:%Y%m%dT%H%M%S}-{slug(row)}-{repeat}"


def gateway(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """A gateway CLI command inside its container, on the main daemon. Output never holds a token."""
    cmd = ["docker", "-H", MAIN_DOCKER, "exec", GATEWAY, "python3", "-I", "/opt/gateway/gateway.py", *args]
    # Its own session, like every docker call here: a Ctrl-C meant for the pilot must not kill a revoke halfway (#259).
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=60, start_new_session=True)


def egress_mode() -> str:
    """What the running egress proxy serves (#236): "open" or "allowlist". It reads this file once, at start, and
    9-live.sh recreates it whenever the file changes (#234), so the copy inside the container is what it enforces.
    A run's lift reads differently under each (METHODOLOGY, live tier), so every record carries it."""
    p = subprocess.run(
        ["docker", "-H", MAIN_DOCKER, "exec", EGRESS_PROXY, "cat", EGRESS_CONFIG],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        start_new_session=True,
    )
    return "open" if json.loads(p.stdout).get("open_egress") is True else "allowlist"


def issue_token(run_id: str, dest: Path) -> None:
    """Issue, copy out, delete inside: the token goes from the gateway's volume to a 0600 file, never to a screen."""
    inside = f"{GATEWAY_STATE}/{run_id}.token"
    gateway("issue", "--config", GATEWAY_CONFIG, "--run", run_id, "--out", inside)
    try:
        subprocess.run(
            ["docker", "-H", MAIN_DOCKER, "cp", f"{GATEWAY}:{inside}", str(dest)],
            capture_output=True,
            check=True,
            timeout=60,
            start_new_session=True,
        )
        os.chmod(dest, 0o600)
    finally:
        subprocess.run(
            ["docker", "-H", MAIN_DOCKER, "exec", GATEWAY, "rm", "-f", inside],
            capture_output=True,
            timeout=60,
            start_new_session=True,
        )


def revoke(run_id: str) -> bool:
    """Revoke the run's token. A failure is recorded and warned about, so the operator can revoke it by hand."""
    try:
        ok = gateway("revoke", "--config", GATEWAY_CONFIG, "--run", run_id, check=False).returncode == 0
    except subprocess.TimeoutExpired:
        ok = False
    if not ok:
        print(f"warning: token for {run_id} was not revoked; run `gateway.py revoke --run {run_id}`", file=sys.stderr)
    return ok


def usage(run_id: str) -> dict | None:
    p = gateway("usage", "--config", GATEWAY_CONFIG, "--run", run_id, check=False)
    try:
        rows = json.loads(p.stdout)
    except ValueError:
        return None
    if not rows:
        return None
    keep = ("requests", "prompt_tokens", "completion_tokens", "unmetered", "refused")
    return {k: rows[0][k] for k in keep}


def read_lift(out: Path) -> dict:
    """What SkillEvaluator wrote: execution status and the overall arm scores and lift, per agent. Nothing derived."""
    found = {}
    for lift in sorted(out.glob("*/*/lift.json")):
        if any(q.is_symlink() for q in (lift, lift.parent, lift.parent.parent)):
            continue  # tier3.copy_report keeps no links; never read through one
        try:
            data = json.loads(lift.read_text())
        except (OSError, ValueError):
            continue
        overall = (data.get("lift") or {}).get("overall") or {}
        found[lift.parent.name] = {
            "execution_status": data.get("execution_status"),
            "with_skill": overall.get("with_skill"),
            "without_skill": overall.get("without_skill"),
            "lift": overall.get("delta"),
        }
    return found


def status_of(
    exit_code: int | None, lift: dict, unsupported: bool = False, stopped: str | None = None
) -> tuple[str, str | None]:
    """completed only when SkillEvaluator says the run succeeded and reports a lift; never a zero for a gap."""
    if unsupported and not lift:
        return "unsupported", "needs a custom environment or dependencies (its Dockerfile is not on the allowlist)"
    if exit_code is None:
        return "failed", stopped or "timed out"
    if not lift:
        return "failed", f"no lift.json (exit {exit_code})"
    agent = next(iter(lift.values()))
    if agent["execution_status"] != "succeeded":
        return "failed", f"execution_status {agent['execution_status']}"
    if agent["lift"] is None:
        return "inconclusive", "no comparable cases for a lift"
    return "completed", None


def record(
    row: dict,
    repeat: int,
    run_id: str,
    res: dict,
    lift: dict,
    counts: dict | None,
    started: str,
    evaluator: dict,
    *,
    egress: str,
    smoke: bool = False,
) -> dict:
    status, reason = status_of(res["exit"], lift, res.get("unsupported", False), res.get("stopped"))
    agent = next(iter(lift.values()), {})
    return {
        "id": run_id,
        "mode": "tier3",
        "pilot": True,
        "source": {
            "repo": row["repo"],
            "url": f"https://github.com/{row['repo']}",
            "skill_path": row["skill_path"],
            "commit": res["commit"],
            "tree_digest": res["tree_digest"],
        },
        "category": row["category"],
        "cases": res["cases"],
        "task_set": row["task_set"],
        "repeat": repeat,
        "evaluator": evaluator,  # read before the trials (#200)
        "agent": tier3.AGENT,
        "model": tier3.AGENT_MODEL or None,
        "judge_model": os.environ.get("SKILL_EVAL_LLM_MODEL") or None,
        "egress": egress,  # "open": the agent could reach public answers; lift is an upper bound (#236)
        "started_at": started,
        "duration_s": res["duration_s"],
        "exit_code": res["exit"],
        "status": status,
        "reason": reason,
        "scores": {k: agent.get(k) for k in ("with_skill", "without_skill", "lift")} if status == "completed" else None,
        "gateway": counts,
        "cost_usd": None,  # the NVIDIA Build trial key is not billed; tokens are in "gateway"
        "report_dir": str(res["out"].relative_to(ev.ROOT)),
        "smoke": smoke,
    }


def crashed(
    row: dict,
    repeat: int,
    run_id: str,
    why: str,
    counts: dict | None,
    started: str,
    evaluator: dict,
    *,
    egress: str,
    smoke: bool = False,
) -> dict:
    """A run that never reached SkillEvaluator's results: failed, with the reason and no scores."""
    return {
        "id": run_id,
        "mode": "tier3",
        "pilot": True,
        "source": {"repo": row["repo"], "url": f"https://github.com/{row['repo']}", "skill_path": row["skill_path"]},
        "category": row["category"],
        "task_set": row["task_set"],
        "repeat": repeat,
        "evaluator": evaluator,
        "agent": tier3.AGENT,
        "model": tier3.AGENT_MODEL or None,
        "egress": egress,
        "started_at": started,
        "status": "failed",
        "reason": why,
        "scores": None,
        "gateway": counts,
        "cost_usd": None,
        "smoke": smoke,
    }


class Stop:
    """What the operator asked for (#259). The handler only sets flags; the loop and the trial's wait read them, so a
    signal never lands in the middle of issuing or revoking a token."""

    def __init__(self):
        self.after_run = self.now = False

    def handle(self, signum, _frame) -> None:
        if signum == signal.SIGTERM or self.after_run:
            self.after_run = self.now = True
            msg = "pilot: stopping the current trial now; it is cleaned up and recorded failed\n"
        else:
            self.after_run = True
            msg = "pilot: stopping after the current run; Ctrl-C again to stop it now\n"
        os.write(2, msg.encode())  # not print: a handler may interrupt a print to stderr


def take_lock() -> int | None:
    """One pilot at a time, and no --cleanup under a running one: an flock, freed by the kernel when we exit."""
    lock = RECORDS.parent / ".lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def cleanup(run_root: Path | None = None) -> int:
    """After a hard kill: what tier3.run and main would have cleaned up, done now. Prints what it did."""
    if os.geteuid() != 0:
        print("cleanup refused: run it as root, from /opt/evalskills (deploy/live/README.md)", file=sys.stderr)
        return 2
    lock = take_lock()
    if lock is None:
        print("cleanup refused: a pilot is still running (it holds the lock)", file=sys.stderr)
        return 2
    try:
        return _cleanup(run_root or tier3.RUN_ROOT)
    finally:
        os.close(lock)


def _cleanup(run_root: Path) -> int:
    problems = []
    stopped = tier3.kill_trial_user(pwd.getpwnam(tier3.TRIAL_USER).pw_uid)
    if not stopped:
        problems.append(f"processes of {tier3.TRIAL_USER} survived SIGKILL; run folders left in place")
    left = tier3.remove_trial_objects()
    if left:
        problems.append(f"not removed: {', '.join(left)} (is the socket proxy up? deploy/live/README.md)")
    try:
        rows = json.loads(gateway("usage", "--config", GATEWAY_CONFIG).stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        rows = None
        problems.append("could not list gateway tokens (is the gateway up?)")
    open_runs = [r["run_id"] for r in rows or [] if not r.get("revoked")]
    revoked = [run_id for run_id in open_runs if revoke(run_id)]
    if len(revoked) < len(open_runs):
        problems.append("some tokens not revoked")
    removed = 0
    if stopped:  # root never reads or removes a folder something of the trial may still write
        for d in sorted(run_root.glob("t3-*")):
            if d.is_dir() and not d.is_symlink():
                try:
                    shutil.rmtree(d)  # fd-based on Linux: never follows a link
                    removed += 1
                except OSError as e:  # say so with the rest, never end before the report of what was done
                    problems.append(f"run folder {d.name} not removed ({type(e).__name__})")
    print(json.dumps({"tokens_revoked": revoked, "run_folders_removed": removed, "problems": problems}))
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    if "--cleanup" in argv:
        return cleanup()
    only = argv[argv.index("--only") + 1] if "--only" in argv else None
    smoke = "--smoke" in argv
    rows = [r for r in load_skills() if only in (None, slug(r))]
    plan = [(r, i) for r in rows for i in range(1, (1 if smoke else r["repeats"]) + 1)]
    max_cases = SMOKE_CASES if smoke else None
    if "--dry-run" in argv:
        for r, i in plan:
            print(f"{slug(r)} #{i} cases={r['task_set'] or 'own'}{f' (first {max_cases} only)' if max_cases else ''}")
        print(f"{len(plan)} runs{', smoke: one case each, both arms' if smoke else ''}")
        return 0
    try:  # before any token or trial: a missing evaluator must not cost a run, or lose its record afterwards (#200)
        evaluator = tier3.evaluator_version()
    except (OSError, subprocess.SubprocessError, RuntimeError) as e:
        print(
            f"pilot stopped: no SkillEvaluator at {ev.EVALUATOR_DIR} ({type(e).__name__}). "
            "Set SKILLEVALUATOR_DIR=/opt/skillevaluator: deploy/live/README.md.",
            file=sys.stderr,
        )
        return 2
    RECORDS.parent.mkdir(parents=True, exist_ok=True)
    lock = take_lock()
    if lock is None:
        print("pilot stopped: another pilot or --cleanup is running (it holds the lock)", file=sys.stderr)
        return 2
    stop = Stop()
    before = {sig: signal.signal(sig, stop.handle) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        return _run_plan(plan, smoke, max_cases, evaluator, stop)
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)
        os.close(lock)


def _run_plan(plan: list, smoke: bool, max_cases: int | None, evaluator: dict, stop: Stop) -> int:
    for n, (row, i) in enumerate(plan):
        if stop.after_run:
            print(f"pilot stopped by the operator: {len(plan) - n} run(s) not started", file=sys.stderr)
            return 130
        now = dt.datetime.now(dt.UTC)
        started = now.isoformat(timespec="seconds")
        run_id = run_id_for(row, i, now, smoke)
        try:  # per run, before its token: the record must say what the trial could reach
            egress = egress_mode()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
            print(
                f"pilot stopped: could not read the egress proxy's config for {run_id} ({type(e).__name__}).",
                file=sys.stderr,
            )
            print("Check that the egress proxy is up: deploy/gvisor/host/9-live.sh.", file=sys.stderr)
            return 2
        with tempfile.TemporaryDirectory(prefix="pilot-") as tmp:
            token_file = Path(tmp) / "token"
            try:
                issue_token(run_id, token_file)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:  # no gateway: stop, plainly
                print(
                    f"pilot stopped: could not issue a gateway token for {run_id} ({type(e).__name__}).",
                    file=sys.stderr,
                )
                print("Check that the gateway container is up: deploy/live/README.md.", file=sys.stderr)
                return 2
            try:
                env = tier3.trial_env({**os.environ, "TIER3_GATEWAY_TOKEN_FILE": str(token_file)})  # Unsafe stops all
                res = tier3.run(
                    row["repo"],
                    row["skill_path"],
                    row["task_set"],
                    env,
                    label=run_id,
                    max_cases=max_cases,
                    should_stop=lambda: stop.now,
                )
                crash = None
            except Exception as e:  # a clone or setup error fails this run, visibly, and the pilot goes on
                res, crash = None, f"{type(e).__name__}: {str(e)[:200]}"
            finally:
                revoked = revoke(run_id)
        if res is None:
            rec = crashed(row, i, run_id, crash, usage(run_id), started, evaluator, egress=egress, smoke=smoke)
        else:
            lift = read_lift(res["out"])
            rec = record(row, i, run_id, res, lift, usage(run_id), started, evaluator, egress=egress, smoke=smoke)
        rec["token_revoked"] = revoked
        with RECORDS.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        print(
            json.dumps(
                {
                    "id": run_id,
                    "status": rec["status"],
                    "reason": rec["reason"],
                    "scores": rec["scores"],
                    "egress": egress,
                }
            )
        )
    if stop.after_run:
        print("pilot stopped by the operator after its last run", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
