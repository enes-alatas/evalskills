"""Self-tests for the repo guards: each still catches what it claims to."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
# Assembled at runtime so this file holds no credential shape itself. Synthetic, never a real token.
FAKE_TOKEN = "ghp_" + "x7Qm2LpR9vK4tB8nW1cZ5hJ3dF6sA0yE" + "uT9o"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "test")
    (tmp_path / "README.md").write_text("hello\n")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-qm", "docs: start")
    return tmp_path


def run_script(name, repo, *args, stdin=None, env=None):
    return subprocess.run(
        [str(SCRIPTS / name), *args],
        cwd=repo,
        input=stdin,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


gitleaks = pytest.mark.skipif(
    not (os.environ.get("GITLEAKS") or shutil.which("gitleaks")), reason="gitleaks not installed"
)


@gitleaks
def test_secret_scan_flags_a_token_added_on_the_branch(repo):
    git(repo, "branch", "base")
    (repo / "config.py").write_text(f'TOKEN = "{FAKE_TOKEN}"\n')
    git(repo, "add", "config.py")
    git(repo, "commit", "-qm", "feat: add config")
    result = run_script("secret-scan.sh", repo, env={"BASE": "base"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert FAKE_TOKEN not in result.stdout + result.stderr, "findings must be printed redacted"


@gitleaks
def test_secret_scan_passes_a_clean_branch(repo):
    git(repo, "branch", "base")
    (repo / "notes.md").write_text("nothing secret here\n")
    git(repo, "add", "notes.md")
    git(repo, "commit", "-qm", "docs: add notes")
    assert run_script("secret-scan.sh", repo, env={"BASE": "base"}).returncode == 0


@gitleaks
def test_secret_scan_staged_blocks_a_token_before_commit(repo):
    (repo / ".env").write_text(f"GH_TOKEN={FAKE_TOKEN}\n")
    git(repo, "add", "-f", ".env")
    assert run_script("secret-scan.sh", repo, "--staged").returncode == 1


@pytest.mark.parametrize(
    ("message", "ok"),
    [
        ("feat(web): add skill form", True),
        ("fix: retry a crashed scanner run", True),
        ("docs(adr)!: record the ranking method", True),
        ("Add skill form", False),
        ("feat(web,pipeline): two scopes", False),
        ("feat: " + "x" * 80, False),
        ("fix: retry scanner\n\nCloses #12", False),
        ("fix: retry scanner\n\nPart of #12", True),
    ],
)
def test_commit_subjects(repo, message, ok):
    result = run_script("commit-subjects.sh", repo, "--stdin", stdin=message + "\0")
    assert (result.returncode == 0) == ok, result.stdout


def test_security_paths_match_what_they_claim(repo):
    changed = "pipeline/server.py\ndeploy/gvisor/firewall.sh\n.github/workflows/ci.yml\nweb/app.css\ndocs/x.md\n"
    result = subprocess.run(
        [str(SCRIPTS / "security-paths.sh")], cwd=ROOT, input=changed, capture_output=True, text=True, check=True
    )
    assert result.stdout.split() == ["pipeline/server.py", "deploy/gvisor/firewall.sh", ".github/workflows/ci.yml"]
