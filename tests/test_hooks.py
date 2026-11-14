"""The git hooks and scripts/check.sh stop what CI would stop, before a push."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ("GITLEAKS", "ACTIONLINT", "SHELLCHECK")
# What scripts/check.sh reads from its environment. CI sets these when it runs check.sh on a push to main, and the
# tests here run check.sh again inside a temp repo, where that BASE commit doesn't exist.
CHECK_INPUTS = ("CI", "BASE", "SUBJECTS", *TOOLS)


def git(repo, *args, env=None, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True, env=env or clean_env()
    )


def clean_env(**extra):
    """The caller's environment without check.sh's inputs (so a missing tool warns, as on a laptop) and git's."""
    env = {k: v for k, v in os.environ.items() if k not in CHECK_INPUTS and not k.startswith("GIT_")}
    return {**env, **extra}


def missing_tools():
    return clean_env(**{name: "/nonexistent/" + name.lower() for name in TOOLS})


def find(name):
    return os.environ.get(name.upper()) or shutil.which(name)


@pytest.fixture
def repo(tmp_path):
    """A clone-like repo with this repo's scripts, the hooks installed, and main as origin/main."""
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "test")
    shutil.copytree(ROOT / "scripts", repo / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "pyproject.toml", repo)  # the ruff settings
    (repo / "README.md").write_text("hello\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_ok.py").write_text("def test_ok():\n    pass\n")  # pytest fails when it collects nothing
    git(repo, "add", "README.md", "pyproject.toml", "scripts", "tests")
    git(repo, "commit", "-qm", "docs: start")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "switch", "-qc", "feat/x")
    result = subprocess.run(
        ["scripts/install-hooks.sh"], cwd=repo, capture_output=True, text=True, check=True, env=clean_env()
    )
    assert "pre-push: installed" in result.stdout
    return repo


def commit(repo, message, path="notes.md", text="x\n", env=None):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git(repo, "add", path, env=env)
    return git(repo, "commit", "-qm", message, env=env, check=False)


def pre_push(repo, ref="refs/heads/feat/x", env=None):
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    line = f"refs/heads/feat/x {head} {ref} {'0' * 40}\n"
    return subprocess.run(
        [str(repo / ".git/hooks/pre-push"), "origin", "url"],
        cwd=repo,
        input=line,
        capture_output=True,
        text=True,
        env=env or missing_tools(),
        check=False,
    )


def test_install_is_idempotent_and_keeps_a_foreign_hook(repo):
    again = subprocess.run(["scripts/install-hooks.sh"], cwd=repo, capture_output=True, text=True, env=clean_env())
    assert again.returncode == 0 and "commit-msg: already installed" in again.stdout
    hook = repo / ".git/hooks/pre-commit"
    hook.unlink()
    hook.write_text("#!/bin/sh\nexit 0\n")
    kept = subprocess.run(["scripts/install-hooks.sh"], cwd=repo, capture_output=True, text=True, env=clean_env())
    assert kept.returncode == 1 and "kept" in kept.stderr
    assert not hook.is_symlink()


def test_commit_msg_refuses_a_wip_subject_with_the_message_ci_prints(repo):
    result = commit(repo, "wip: half done", env=missing_tools())
    assert result.returncode != 0
    assert "not a Conventional Commit subject: wip: half done" in result.stdout + result.stderr


def test_commit_msg_allows_a_fixup_so_autosquash_works(repo):
    assert commit(repo, "docs: notes", env=missing_tools()).returncode == 0
    result = commit(repo, "fixup! docs: notes", text="y\n", env=missing_tools())
    assert result.returncode == 0, result.stderr


def test_commit_msg_ignores_the_diff_that_commit_v_appends(repo):
    message = repo / "MSG"
    scissors = "# " + "-" * 24 + " >8 " + "-" * 24
    message.write_text(f"docs: notes\n\n{scissors}\n# Do not modify or remove the line above.\n+fixes #12\n")
    hook = subprocess.run([str(repo / ".git/hooks/commit-msg"), str(message)], cwd=repo, capture_output=True, text=True)
    assert hook.returncode == 0, hook.stdout
    message.write_text("docs: notes\n\nfixes #12\n")
    hook = subprocess.run([str(repo / ".git/hooks/commit-msg"), str(message)], cwd=repo, capture_output=True, text=True)
    assert hook.returncode == 1 and "closing keyword" in hook.stdout


def test_a_missing_tool_warns_and_the_commit_goes_through(repo):
    result = commit(repo, "fix: a python file", path="a.py", text="x = 1\n", env=missing_tools())
    assert result.returncode == 0, result.stderr
    assert "gitleaks not found" in result.stderr


def test_pre_push_refuses_an_unsquashed_fixup(repo):
    commit(repo, "docs: notes", env=missing_tools())
    commit(repo, "fixup! docs: notes", text="y\n", env=missing_tools())
    result = pre_push(repo)
    assert result.returncode == 1
    assert "unsquashed commit: fixup! docs: notes" in result.stdout
    assert "FAILED: commit subjects" in result.stderr
    assert "skipped here" in result.stderr, "missing tools are named, not silently passed"


def test_pre_push_to_a_diag_branch_skips_the_subject_check(repo):
    commit(repo, "docs: notes", env=missing_tools())
    commit(repo, "fixup! docs: notes", text="y\n", env=missing_tools())
    assert pre_push(repo, ref="refs/heads/diag/try").returncode == 0


def test_in_ci_a_missing_tool_fails(repo):
    result = subprocess.run(
        ["scripts/check.sh", "--fast"], cwd=repo, capture_output=True, text=True, env={**missing_tools(), "CI": "true"}
    )
    assert result.returncode == 1
    assert "gitleaks not found; CI must have it" in result.stderr


@pytest.mark.skipif(not (shutil.which("ruff") or shutil.which("uvx")), reason="ruff not available")
def test_pre_push_refuses_a_lint_error(repo):
    (repo / "a.py").write_text("import os\n")  # unused import, F401
    git(repo, "add", "a.py")
    git(repo, "commit", "-qm", "feat: a", "--no-verify")
    result = pre_push(repo)
    assert result.returncode == 1
    assert "FAILED: ruff check" in result.stderr


@pytest.mark.skipif(not (shutil.which("ruff") or shutil.which("uvx")), reason="ruff not available")
def test_pre_push_refuses_a_failing_test(repo):
    (repo / "tests/test_a.py").write_text("def test_a():\n    assert 1 == 2\n")
    git(repo, "add", "tests")
    git(repo, "commit", "-qm", "test: a", "--no-verify")
    result = pre_push(repo)
    assert result.returncode == 1
    assert "FAILED: tests" in result.stderr


@pytest.mark.skipif(not (find("actionlint") and find("shellcheck")), reason="actionlint or shellcheck not installed")
def test_actionlint_catches_a_shell_syntax_error_in_a_run_block(repo):
    workflow = (
        "name: w\non: push\njobs:\n  j:\n    runs-on: ubuntu-24.04\n    steps:\n"
        "      - run: |\n          if true; then echo 'a'\n"
    )
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/w.yml").write_text(workflow)
    git(repo, "add", ".github")
    git(repo, "commit", "-qm", "ci: w", "--no-verify")
    env = clean_env(ACTIONLINT=find("actionlint"), SHELLCHECK=find("shellcheck"), GITLEAKS="/nonexistent/gitleaks")
    result = subprocess.run(["scripts/check.sh", "--fast"], cwd=repo, capture_output=True, text=True, env=env)
    assert result.returncode == 1
    assert "FAILED: actionlint" in result.stderr, result.stdout


def test_ci_runs_check_sh_with_the_versions_it_pins():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    check = (ROOT / "scripts/check.sh").read_text()
    assert "run: scripts/check.sh" in ci
    for step in ("ruff check", "ruff format", "commit-subjects.sh", "secret-scan.sh"):
        assert f"run: {step}" not in ci and f"run: scripts/{step}" not in ci, f"{step} must run through check.sh"
    pins = dict(re.findall(r"^([A-Z]+_VERSION)=(\S+)$", check, re.M))
    for name in ("GITLEAKS_VERSION", "ACTIONLINT_VERSION", "SHELLCHECK_VERSION"):
        assert f"{name}: {pins[name]}" in ci
    assert f"ruff=={pins['RUFF_VERSION']} pytest=={pins['PYTEST_VERSION']}" in ci
    paths = (ROOT / ".github/security-review-paths").read_text().split()
    assert "scripts/check.sh" in paths and "scripts/hooks/*" in paths, "the gates CI runs need a security review"
    assert f"ruff@{pins['RUFF_VERSION']}" in (ROOT / "scripts/hooks/pre-commit").read_text()


def test_the_hook_tests_ignore_the_check_inputs_ci_sets_on_a_push(monkeypatch):
    for name, value in {"CI": "true", "BASE": "0" * 40, "SUBJECTS": "0"}.items():
        monkeypatch.setenv(name, value)
    assert not set(CHECK_INPUTS) & set(clean_env())
