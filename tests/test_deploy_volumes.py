"""Deploy image runtime checks: volume ownership (#71) and a frozen evaluator venv (#75)."""

import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def test_every_compose_mount_point_is_created_owned_by_app():
    compose = (DEPLOY / "compose.yaml").read_text()
    mounts = re.findall(r"^\s+- \w+:(/home/app/\S+)", compose, re.M)
    assert len(mounts) == 4
    docker = (DEPLOY / "Dockerfile").read_text().replace("\\\n", " ")
    (created,) = re.findall(r"RUN install -d -o app -g app ([^\n]+)", docker)
    assert set(mounts) <= set(created.split())
    # The install runs as root, and the image switches back to app before it starts.
    users = re.findall(r"^USER (\w+)", docker, re.M)
    assert users[-1] == "app"


def test_runtime_uv_never_resyncs_the_evaluator():
    # #75: on the read-only root, a runtime `uv run` that re-syncs or rebuilds the editable evaluator fails. The image
    # sets UV_NO_SYNC, and the evaluator subprocess env must let it through.
    import sys

    sys.path.insert(0, str(DEPLOY.parent / "pipeline"))
    import evaluate

    docker = (DEPLOY / "Dockerfile").read_text()
    assert re.search(r"^ENV UV_NO_SYNC=1\b", docker, re.M)
    assert {"UV_NO_SYNC", "UV_FROZEN", "UV_PYTHON_PREFERENCE"} <= evaluate.ENV_ALLOW


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
