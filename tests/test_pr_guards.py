"""#176: pr-guards labels a PR from this repository, and on a fork PR lists the paths and passes without labelling."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pr-guards.yml"


def step_script() -> str:
    """The `run: |` block of the labelling step, dedented, as the runner's bash gets it."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "run: |") + 1
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = []
    for line in lines[start:]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line[indent:])
    return "\n".join(body) + "\n"


@pytest.fixture
def runner(tmp_path):
    (tmp_path / "scripts").mkdir()
    paths = tmp_path / "scripts" / "security-paths.sh"
    paths.write_text("#!/bin/sh\ncat >/dev/null\necho gateway/docker_proxy.py\n")
    paths.chmod(0o755)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    gh = bin_ / "gh"
    gh.write_text(
        '#!/bin/sh\necho "$*" >> "$GH_LOG"\n[ "$1 $2" = "pr diff" ] && echo gateway/docker_proxy.py\nexit 0\n'
    )
    gh.chmod(0o755)

    def run(head_repo: str):
        log, summary = tmp_path / "gh.log", tmp_path / "summary.md"
        for f in (log, summary):
            f.write_text("")
        env = {
            **os.environ,
            "PATH": f"{bin_}:{os.environ['PATH']}",
            "GH_LOG": str(log),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_BASE_REF": "main",
            "PR": "7",
            "REPO": "enes-alatas/evalskills",
            "HEAD_REPO": head_repo,
        }
        p = subprocess.run(["bash", "-e", "-c", step_script()], cwd=tmp_path, env=env, capture_output=True, text=True)
        return p.returncode, log.read_text(), summary.read_text()

    return run


def test_a_pr_from_this_repository_is_labelled(runner):
    code, gh, summary = runner("enes-alatas/evalskills")
    assert code == 0
    assert "pr edit 7 --repo enes-alatas/evalskills --add-label needs-security-review" in gh
    assert "gateway/docker_proxy.py" in summary


def test_a_fork_pr_lists_the_paths_and_passes_without_labelling(runner):
    code, gh, summary = runner("someone/evalskills")
    assert code == 0
    assert "pr edit" not in gh
    assert "gateway/docker_proxy.py" in summary and "by hand" in summary
