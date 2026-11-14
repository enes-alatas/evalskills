"""#123/#137: the withdrawn daemon's uninstall.sh, checked without a host (stub binaries)."""

import os
import subprocess
from pathlib import Path

import pytest

GVISOR = Path(__file__).resolve().parents[1] / "deploy" / "gvisor"
SCOPE = "172.31.240.0/20"


@pytest.mark.parametrize("script", ["uninstall.sh"])
def test_scripts_parse(script):
    subprocess.run(["sh", "-n", str(GVISOR / script)], check=True, timeout=30)


# Anything that would change a machine: if a guard regresses, the script fails loudly here and touches nothing.
DANGEROUS = (
    "curl",
    "gpg",
    "apt-get",
    "systemctl",
    "groupadd",
    "groupdel",
    "userdel",
    "install",
    "iptables",
    "iptables-save",
    "rm",
    "sed",
    "docker",
    "dpkg",
)


def stub_bin(tmp_path, **bodies):
    for name in DANGEROUS:
        (tmp_path / name).write_text("#!/bin/sh\nexit 99\n")
        (tmp_path / name).chmod(0o755)
    for name, body in bodies.items():
        (tmp_path / name).write_text(f"#!/bin/sh\n{body}\n")
        (tmp_path / name).chmod(0o755)
    return {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}


def test_uninstall_is_a_dry_run_without_yes(tmp_path):
    """#138: it says what it touches first, and acts only with --yes."""
    env = stub_bin(tmp_path, id="echo 0", ip="exit 99")
    p = subprocess.run(["sh", str(GVISOR / "uninstall.sh")], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert p.returncode == 0, p.stderr
    assert "Dry run" in p.stdout and "br-evals" in p.stdout and "EVALS-INPUT" in p.stdout


def test_uninstall_needs_root(tmp_path):
    env = stub_bin(tmp_path, id="echo 1000", ip="exit 99")
    p = subprocess.run(["sh", str(GVISOR / "uninstall.sh"), "--yes"], env=env, capture_output=True, text=True)
    assert p.returncode == 1 and "root" in p.stderr


def test_uninstall_only_touches_the_eval_range():
    text = (GVISOR / "uninstall.sh").read_text()
    deletes = [line for line in text.splitlines() if "iptables -D" in line]
    assert deletes and all('-s "$SCOPE"' in line for line in deletes)
    assert "SCOPE=172.31.240.0/20" in text
    assert "iptables-save" in text and "diff" in text


def test_uninstall_leaves_dockremap_and_a_running_daemon_alone(tmp_path):
    """#138 review: dockremap isn't uniquely ours, and stopping a running eval daemon is its own announced step."""
    text = (GVISOR / "uninstall.sh").read_text()
    assert "userdel" not in text.replace("Then: userdel", "")
    env = stub_bin(tmp_path, id="echo 0", ip="exit 99", systemctl='[ "$1" = is-active ] && exit 0; exit 99')
    p = subprocess.run(["sh", str(GVISOR / "uninstall.sh"), "--yes"], env=env, capture_output=True, text=True)
    assert p.returncode == 1 and "own announced step" in p.stderr


@pytest.mark.parametrize(
    ("mounts", "refused"),
    [
        ("overlay /var/lib/docker-evals/overlay2/abc/merged overlay rw 0 0\n", True),
        ("tmpfs /var/lib/docker-evals tmpfs rw 0 0\n", True),
        ("overlay /var/lib/docker-evals2/x overlay rw 0 0\n/dev/sda1 / ext4 rw 0 0\n", False),
    ],
)
def test_uninstall_refuses_with_mounts_under_the_data_root(tmp_path, mounts, refused):
    """#138 review: a leftover overlay mount below the data root must stop rm -rf, not only a mount on it."""
    (tmp_path / "mounts").write_text(mounts)
    env = stub_bin(tmp_path, id="echo 0", ip="exit 99", systemctl='[ "$1" = is-active ] && exit 3; exit 99')
    env["MOUNTS_FILE"] = str(tmp_path / "mounts")
    p = subprocess.run(["sh", str(GVISOR / "uninstall.sh"), "--yes"], env=env, capture_output=True, text=True)
    if refused:
        assert p.returncode == 1 and "still mounted" in p.stderr
    else:
        assert "still mounted" not in p.stderr  # it goes on, into the stubbed docker call, and stops there
