"""The internal live pilot (#121): run pilot/skills.tsv through Tier 3, one run at a time, privately.

Each run gets its own gateway token, issued and copied without being printed, and revoked when the run ends. The
gateway's counts for that run (requests, tokens) go into its record. Records go to reports/pilot/results.jsonl,
which is never committed and never reaches the public build (ADR-0007: the board shows no pilot result; E12 in
docs/EXPERIMENT_LOG.md publishes per-skill results).

Run on the VPS, as root from /opt/evalskills, after deploy/gvisor/ and deploy/live/ are set up. SkillEvaluator itself
runs as the trial user (tier3.TRIAL_USER), through the socket proxy:
  SKILLEVALUATOR_DIR=/opt/skillevaluator DOCKER_HOST=unix:///run/evalskills/docker.sock \\
  TIER3_GATEWAY_URL=http://evals-gateway:8443/v1 TIER3_AGENT_MODEL=nvidia/nemotron-3-super-120b-a12b \\
  SKILL_EVAL_LLM_MODEL=nvidia/nemotron-3-super-120b-a12b \\
  python3 -I pipeline/live_pilot.py [--only SLUG] [--dry-run]
"""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate as ev  # noqa: E402
import tier3  # noqa: E402

SKILLS = ev.ROOT / "pilot" / "skills.tsv"  # not under catalog/: the server reads every TSV there
RECORDS = ev.REPORTS / "pilot" / "results.jsonl"
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


def run_id_for(row: dict, repeat: int, now: dt.datetime) -> str:
    """Unique per run: the gateway refuses a reused run ID, so a same-day retry needs the time, not just the date."""
    return f"pilot-{now:%Y%m%dT%H%M%S}-{slug(row)}-{repeat}"


def gateway(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """A gateway CLI command inside its container, on the main daemon. Output never holds a token."""
    cmd = ["docker", "-H", MAIN_DOCKER, "exec", GATEWAY, "python3", "-I", "/opt/gateway/gateway.py", *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=60)


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
        )
        os.chmod(dest, 0o600)
    finally:
        subprocess.run(
            ["docker", "-H", MAIN_DOCKER, "exec", GATEWAY, "rm", "-f", inside], capture_output=True, timeout=60
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
    }


def crashed(
    row: dict, repeat: int, run_id: str, why: str, counts: dict | None, started: str, evaluator: dict, *, egress: str
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
    }


def main(argv: list[str]) -> int:
    only = argv[argv.index("--only") + 1] if "--only" in argv else None
    rows = [r for r in load_skills() if only in (None, slug(r))]
    plan = [(r, i) for r in rows for i in range(1, r["repeats"] + 1)]
    if "--dry-run" in argv:
        for r, i in plan:
            print(f"{slug(r)} #{i} cases={r['task_set'] or 'own'}")
        print(f"{len(plan)} runs")
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
    for row, i in plan:
        now = dt.datetime.now(dt.UTC)
        started = now.isoformat(timespec="seconds")
        run_id = run_id_for(row, i, now)
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
                res, crash = tier3.run(row["repo"], row["skill_path"], row["task_set"], env), None
            except Exception as e:  # a clone or setup error fails this run, visibly, and the pilot goes on
                res, crash = None, f"{type(e).__name__}: {str(e)[:200]}"
            finally:
                revoked = revoke(run_id)
        if res is None:
            rec = crashed(row, i, run_id, crash, usage(run_id), started, evaluator, egress=egress)
        else:
            rec = record(row, i, run_id, res, read_lift(res["out"]), usage(run_id), started, evaluator, egress=egress)
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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
