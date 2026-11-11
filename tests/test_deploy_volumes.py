"""Deploy image runtime checks: volume ownership (#71) and the scanner sidecar (#65)."""

import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def test_every_compose_mount_point_is_created_owned_by_app():
    compose = (DEPLOY / "compose.yaml").read_text()
    mounts = re.findall(r"^\s+- [\w-]+:(/home/app/\S+)", compose, re.M)
    assert len(mounts) == 5
    docker = (DEPLOY / "Dockerfile").read_text().replace("\\\n", " ")
    (created,) = re.findall(r"RUN install -d -o app -g app ([^\n]+)", docker)
    assert set(mounts) <= set(created.split())
    # The install runs as root, and the image switches back to app before it starts.
    users = re.findall(r"^USER (\w+)", docker, re.M)
    assert users[-1] == "app"


def test_app_image_runs_no_evaluator():
    # #65: zone A neither installs SkillEvaluator nor keeps a uv cache a scan could plant code in.
    docker = (DEPLOY / "Dockerfile").read_text()
    compose = (DEPLOY / "compose.yaml").read_text()
    assert "SkillEvaluator.git" not in docker and "uv " not in docker
    assert "uvcache" not in compose and "UV_CACHE_DIR" not in compose


TIER1 = re.compile(r"ghcr\.io/nvidia/skillevaluator-tier1@sha256:[0-9a-f]{64}")


def test_one_tier1_image_digest_everywhere():
    files = [
        DEPLOY / "compose.yaml",
        DEPLOY / "scanner" / "Dockerfile",
        DEPLOY.parent / ".github/workflows/skills.yml",
        DEPLOY.parent / ".github/workflows/engine-parity.yml",
    ]
    found = {f.name: set(TIER1.findall(f.read_text())) for f in files}
    assert all(len(v) == 1 for v in found.values()), found
    assert len(set().union(*found.values())) == 1, found


def _service(compose: str, name: str) -> str:
    m = re.search(rf"^  {name}:\n(.*?)(?=^  \w[\w-]*:\n|^\S)", compose, re.M | re.S)
    assert m, name
    return m.group(1)


def test_scanner_service_is_contained():
    compose = (DEPLOY / "compose.yaml").read_text()
    scanner = _service(compose, "scanner")
    for line in ("network_mode: none", "read_only: true", "cap_drop: [ALL]", "restart: always", 'user: "10001:10001"'):
        assert line in scanner, line
    assert "no-new-privileges:true" in scanner and "pids_limit" in scanner and "mem_limit" in scanner
    assert "env_file" not in scanner and "ports" not in scanner and "networks" not in scanner
    assert "scan-in:/scan/in:ro" in scanner
    # Only the two exchange volumes: never the app's data, reports or clones.
    assert sorted(re.findall(r"^\s+- ([\w-]+):", scanner, re.M)) == ["scan-in", "scan-out"]
    assert not re.search(r"^\s+- [^#\n]*docker\.sock", compose, re.M)  # no service mounts a Docker socket


def test_failed_tool_stderr_reaches_the_log(capsys):
    import subprocess
    import sys

    sys.path.insert(0, str(DEPLOY.parent / "pipeline"))
    import evaluate

    try:
        evaluate.sh([sys.executable, "-I", "-c", "import sys; sys.stderr.write('boom-detail'); sys.exit(3)"])
    except subprocess.CalledProcessError:
        pass
    else:
        raise AssertionError("expected CalledProcessError")
    assert "boom-detail" in capsys.readouterr().err
