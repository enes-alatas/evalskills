"""Tier 3 (live) evaluation. Prepared, not yet run: needs a provider key + sandbox.

We copy the skill snapshot and run `skillevaluator tier3 evaluate` with the baseline arm on. By default the skill
runs on SkillEvaluator's own content (ADR-0006): its published evals/, or the starter case SkillEvaluator prepares
when there are none. Those results are per-skill Skill Lift and not comparable across skills.

With TASK_SET, the skill's evals/ are replaced by shared-tasks/<set>/ so every skill in that set faces the same
cases. A set is accepted only if it is vendored from a public benchmark and carries a SOURCE.md saying so.

Usage (once unblocked):
  DOCKER_HOST=unix:///run/docker-evals.sock TIER3_GATEWAY_URL=... TIER3_GATEWAY_TOKEN_FILE=... \
  python3 -I pipeline/tier3.py OWNER/REPO SKILL_PATH [TASK_SET] [--dry-run]
It refuses to start on any other daemon or env mode, with a real provider key in its environment, or without a
gateway token (see trial_env).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate as ev  # noqa: E402

SHARED = ev.ROOT / "shared-tasks"
TIER3_TIMEOUT = int(os.environ.get("TIER3_TIMEOUT", "3600"))
AGENT = os.environ.get("TIER3_AGENT", "opencode")  # nv_build default agent
ENV_MODE = os.environ.get("TIER3_ENV_MODE", "docker")  # never "local" for untrusted skills
N_ATTEMPTS = os.environ.get("TIER3_N_ATTEMPTS", "1")
EVALS_SOCKET = "unix:///run/docker-evals.sock"  # the gVisor daemon (ADR-0003, deploy/gvisor/)
REAL_KEYS = ("ANTHROPIC_API_KEY", "NVIDIA_API_KEY", "OPENAI_API_KEY", "SKILL_EVAL_LLM_API_KEY")


class Unsafe(SystemExit):
    pass


def trial_env(environ=os.environ) -> dict:
    """The environment for the SkillEvaluator → agent chain, or Unsafe before anything runs or spends.

    ADR-0003: trials run only on the gVisor docker-evals daemon. ADR-0004: this chain never holds a real provider
    key; it gets the model gateway's URL and a per-run token (read from a file the gateway issues), as SkillEvaluator's
    openai-compatible provider."""
    if ENV_MODE != "docker":
        raise Unsafe(f"refusing --env-mode {ENV_MODE}: only docker on the docker-evals daemon is approved (ADR-0003)")
    if environ.get("DOCKER_HOST") != EVALS_SOCKET:
        raise Unsafe(f"refusing: DOCKER_HOST must be {EVALS_SOCKET}, not the host's main Docker daemon (ADR-0003)")
    held = [k for k in REAL_KEYS if environ.get(k)]
    if held:
        raise Unsafe(f"refusing: {', '.join(held)} is set; real keys live only in the gateway (ADR-0004)")
    url, token_file = environ.get("TIER3_GATEWAY_URL"), environ.get("TIER3_GATEWAY_TOKEN_FILE")
    if not url or not token_file:
        raise Unsafe("refusing: set TIER3_GATEWAY_URL and TIER3_GATEWAY_TOKEN_FILE (a per-run gateway token)")
    try:
        token = Path(token_file).read_text().strip()
    except OSError as e:
        raise Unsafe(f"refusing: cannot read TIER3_GATEWAY_TOKEN_FILE ({e.strerror})") from e
    if not token:
        raise Unsafe("refusing: TIER3_GATEWAY_TOKEN_FILE is empty")
    env = ev.git_env()
    env.update(
        {
            "DOCKER_HOST": EVALS_SOCKET,
            "SKILL_EVAL_LLM_PROVIDER": "openai-compatible",
            "SKILL_EVAL_LLM_BASE_URL": url,
            "SKILL_EVAL_LLM_API_KEY": token,
        }
    )
    if environ.get("SKILL_EVAL_LLM_MODEL"):
        env["SKILL_EVAL_LLM_MODEL"] = environ["SKILL_EVAL_LLM_MODEL"]
    return env


# What a skill may not bring to its own live run (ADR-0006): anything that grades it, configures grading, or shapes
# its trial environment. Its cases (evals.json/jsonl/yaml) and their input files stay; SkillEvaluator's default
# judge scores them, and the result is labelled self-reported.
OWN_EVALS_STRIPPED = ("grader.py", "grader.sh", "tests", "harbor", "config.yml", "config.yaml", "environment")
CASES_OWN = "self-reported"  # the skill's own cases, default judge
CASES_SHARED = "shared"  # a vendored public set


def strip_own_grading(evals: Path) -> list[str]:
    """Remove a skill's own graders, grading config and trial environment from its evals/. Returns what went."""
    gone = []
    for name in OWN_EVALS_STRIPPED:
        p = evals / name
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p)
        else:
            continue
        gone.append(name)
    return gone


def prepare(skill_dir: Path, task_set: str | None, dest: Path) -> Path:
    """Copy the skill to dest. Without a task set it stays as published; with one, the set replaces its evals/."""
    if task_set:
        src = SHARED / task_set
        if not (src / "evals.json").is_file():
            raise ValueError(f"no shared task set {task_set}")
        if not (src / "SOURCE.md").is_file():
            raise ValueError(f"task set {task_set} has no SOURCE.md naming its public benchmark (ADR-0006)")
    shutil.copytree(skill_dir, dest, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    if not task_set:
        evals = dest / "evals"
        if evals.is_symlink():
            evals.unlink()  # a linked evals/ could point anywhere; the skill then runs on SkillEvaluator's starter case
        elif evals.is_dir():
            strip_own_grading(evals)
        return dest
    shutil.rmtree(dest / "evals", ignore_errors=True)  # a shared set replaces the skill's own cases
    (dest / "evals").mkdir()
    data = json.loads((src / "evals.json").read_text())
    data["skill_name"] = dest.name
    for e in data["evals"]:
        if "expected_skill" not in e:
            e["expected_skill"] = dest.name
    (dest / "evals" / "evals.json").write_text(json.dumps(data, indent=1))
    for name in ("grader.py", "config.yml"):
        if (src / name).is_file():
            shutil.copy2(src / name, dest / "evals" / name)
    if (src / "files").is_dir():
        shutil.copytree(src / "files", dest / "evals" / "files")
    return dest


def command(skill_dir: Path, out: Path, repo: str, commit: str) -> list[str]:
    return [
        ev.UV,
        "run",
        "-q",
        "--python",
        "3.13",
        "--extra",
        "tier3",
        "skillevaluator",
        "tier3",
        "evaluate",
        str(skill_dir),
        "--agents",
        AGENT,
        "--env-mode",
        ENV_MODE,
        "--n-attempts",
        N_ATTEMPTS,
        "--evaluated-source-repository",
        repo,
        "--evaluated-source-revision",
        commit,
        "--results-dir",
        str(out),
    ]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    repo, skill_path = args[0], args[1]
    task_set = args[2] if len(args) > 2 else None
    dry = "--dry-run" in sys.argv
    env = None if dry else trial_env()  # refuse unsafe setups before cloning or spending anything
    with ev.GIT_LOCK:
        repo_dir, commit = ev.checkout(repo)
    tmp = Path(tempfile.mkdtemp(prefix="t3-"))
    try:
        skill = prepare(repo_dir / skill_path, task_set, tmp / Path(skill_path).name)
        cases = CASES_SHARED if task_set else CASES_OWN
        out = (
            ev.REPORTS
            / f"{repo.replace('/', '__')}__{skill_path.replace('/', '__')}"
            / f"{commit[:12]}-tier3-{task_set or 'own'}"
        )
        cmd = command(skill, out, repo, commit)
        if dry:
            evals = sorted(p.name for p in (skill / "evals").iterdir()) if (skill / "evals").is_dir() else []
            print(json.dumps({"cmd": cmd, "cases": cases, "evals": evals}, indent=1))
            return
        out.mkdir(parents=True, exist_ok=True)
        p = subprocess.run(cmd, cwd=ev.EVALUATOR_DIR, env=env, capture_output=True, text=True, timeout=TIER3_TIMEOUT)
        (out / "run.log").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
        (out / "cases.json").write_text(json.dumps({"cases": cases, "task_set": task_set}))
        print(json.dumps({"exit": p.returncode, "cases": cases, "out": str(out)}))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
