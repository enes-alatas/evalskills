"""Tier 3 (live) evaluation on SHARED category tasks. Prepared, not yet run: needs a provider key + sandbox.

For a skill in a category that has a task set under shared-tasks/<set>/, we copy the skill snapshot,
replace its evals/ with the shared set (evals.json, grader.py, config.yml, files/), and run
`skillevaluator tier3 evaluate` with the baseline arm on. Every skill in that set then faces the same
cases + the same deterministic grader, which is what makes their live scores comparable.

Usage (once unblocked):
  python3 -I pipeline/tier3.py OWNER/REPO SKILL_PATH TASK_SET [--dry-run]
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


def prepare(skill_dir: Path, task_set: str, dest: Path) -> Path:
    src = SHARED / task_set
    if not (src / "evals.json").is_file():
        raise ValueError(f"no shared task set {task_set}")
    shutil.copytree(skill_dir, dest, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    shutil.rmtree(dest / "evals", ignore_errors=True)  # the skill's own cases are not used for ranking
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
    if ENV_MODE == "local":
        raise SystemExit("refusing local mode: untrusted skills must run in an isolated sandbox")
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
    repo, skill_path, task_set = args
    with ev.GIT_LOCK:
        repo_dir, commit = ev.checkout(repo)
    tmp = Path(tempfile.mkdtemp(prefix="t3-"))
    try:
        skill = prepare(repo_dir / skill_path, task_set, tmp / Path(skill_path).name)
        out = (
            ev.REPORTS
            / f"{repo.replace('/', '__')}__{skill_path.replace('/', '__')}"
            / f"{commit[:12]}-tier3-{task_set}"
        )
        cmd = command(skill, out, repo, commit)
        if "--dry-run" in sys.argv:
            print(json.dumps({"cmd": cmd, "evals": sorted(p.name for p in (skill / "evals").iterdir())}, indent=1))
            return
        env = ev.git_env()
        env.update({k: v for k, v in os.environ.items() if k in ev.PROVIDER_VARS or k == "DOCKER_HOST"})
        out.mkdir(parents=True, exist_ok=True)
        p = subprocess.run(cmd, cwd=ev.EVALUATOR_DIR, env=env, capture_output=True, text=True, timeout=TIER3_TIMEOUT)
        (out / "run.log").write_text(p.stdout + "\n--- stderr ---\n" + p.stderr)
        print(json.dumps({"exit": p.returncode, "out": str(out)}))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
