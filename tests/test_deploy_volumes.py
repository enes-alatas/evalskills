"""Every volume mount in deploy/compose.yaml must exist in the image, owned by the app user (#71)."""

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
