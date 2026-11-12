"""#137: SkillEvaluator --env-mode gvisor. Needs SkillEvaluator and Harbor, so it runs where they are installed:

  cd $SKILLEVALUATOR_DIR && uv run -q --python 3.13 --extra tier3 --with pytest --with-editable <repo>/gvisor_env \\
    python -m pytest <repo>/tests/test_gvisor_env_mode.py

Elsewhere (CI, the plain test run) it is skipped."""

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("skillevaluator")
pytest.importorskip("evalskills_gvisor")

import evalskills_gvisor as g  # noqa: E402
from harbor.models.task.config import EnvironmentConfig  # noqa: E402
from harbor.models.trial.paths import TrialPaths  # noqa: E402
from skillevaluator.tier3.harbor.secure_docker_environment import (  # noqa: E402
    SkillEvaluatorSecureDockerEnvironment,
)
from skillevaluator.tier3_environments import is_env_mode, load_custom_backend  # noqa: E402

PREBUILT = "example.invalid/trial@sha256:" + "0" * 64


def env(tmp_path: Path, image: str | None = PREBUILT, **kwargs) -> g.GvisorEnvironment:
    d = tmp_path / "environment"
    d.mkdir(exist_ok=True)
    (d / "Dockerfile").write_text("FROM scratch\n")
    return g.GvisorEnvironment(
        environment_dir=d,
        environment_name="gvisor-test",
        session_id="gvisor-test",
        trial_paths=TrialPaths(tmp_path / "trial"),
        task_env_config=EnvironmentConfig(docker_image=image),
        **kwargs,
    )


def test_the_mode_is_registered_through_the_entry_point():
    assert is_env_mode("gvisor")
    spec = load_custom_backend("gvisor")
    assert spec is g.SPEC and spec.import_path == "evalskills_gvisor:GvisorEnvironment"
    assert not spec.environment_kwargs  # operators can't pass a runtime or DNS


def test_every_service_gets_runsc_and_no_resolver(tmp_path):
    e = env(tmp_path, kata_runtime="runc", kata_dns=["1.1.1.1"])  # ignored: forced in __init__
    overlay = json.loads(e._ensure_runtime_compose_file().read_text())
    assert overlay["services"] and all(s["runtime"] == "runsc" for s in overlay["services"].values())
    resolv = Path(overlay["services"]["main"]["volumes"][0]["source"]).read_text()
    assert resolv == "nameserver 127.0.0.1\n"
    assert e._docker_compose_paths[-1] == e._ensure_runtime_compose_file()  # last, so a task file can't override it


def test_the_hardened_backend_still_does_the_work(tmp_path):
    e = env(tmp_path)
    owner = next(c for c in type(e).__mro__ if "exec" in c.__dict__)
    assert owner is SkillEvaluatorSecureDockerEnvironment
    assert not e._requires_egress_control()


def test_preflight_refuses_without_runsc(monkeypatch):
    monkeypatch.setattr(g.GvisorEnvironment, "_docker_runtimes", classmethod(lambda cls: ["runc"]))
    with pytest.raises(SystemExit, match="runsc is not registered"):
        g.GvisorEnvironment.preflight()


def started(monkeypatch, tmp_path, runtimes, e=None):
    """Start with Harbor's own start stubbed; returns (raised error or None, whether the trial was torn down)."""
    e = e or env(tmp_path)
    stopped = []

    async def nothing(self, force_build):
        return None

    async def stop(self, delete):
        stopped.append(delete)

    monkeypatch.setattr(SkillEvaluatorSecureDockerEnvironment, "start", nothing)
    monkeypatch.setattr(g.GvisorEnvironment, "stop", stop)
    monkeypatch.setattr(g.GvisorEnvironment, "_project_runtimes", lambda self: runtimes(self))
    try:
        asyncio.run(e.start(force_build=False))
    except RuntimeError as err:
        return err, stopped
    return None, stopped


def test_start_passes_when_every_container_is_under_runsc(monkeypatch, tmp_path):
    err, stopped = started(monkeypatch, tmp_path, lambda e: {"/main": "runsc", "/db": "runsc"})
    assert err is None and stopped == []


@pytest.mark.parametrize(
    "runtimes",
    [
        lambda e: {"/main": "runsc", "/db": "runc"},  # a service the overlay missed
        lambda e: {"/main": ""},  # the daemon's default
        lambda e: {},  # nothing to check: refuse, don't assume
    ],
)
def test_start_tears_down_anything_not_under_runsc(monkeypatch, tmp_path, runtimes):
    err, stopped = started(monkeypatch, tmp_path, runtimes)
    assert err is not None and "refused the trial" in str(err)
    assert stopped == [True]


def test_start_tears_down_when_docker_cannot_be_asked(monkeypatch, tmp_path):
    def broken(e):
        raise subprocess.CalledProcessError(1, "docker")

    err, stopped = started(monkeypatch, tmp_path, broken)
    assert err is not None and "could not inspect" in str(err) and stopped == [True]


def test_an_included_service_is_refused_before_start(monkeypatch, tmp_path):
    """#139 review: Harbor's overlay doesn't follow a local include:, so that service would run under runc."""
    e = env(tmp_path)
    (e.environment_dir / "docker-compose.yaml").write_text("include:\n  - extra.yaml\nservices:\n  main: {}\n")
    (e.environment_dir / "extra.yaml").write_text("services:\n  side:\n    image: busybox\n")
    err, stopped = started(monkeypatch, tmp_path, lambda e: pytest.fail("must not get as far as start"), e)
    assert err is not None and "not pinned to runsc: side" in str(err)
    assert stopped == []  # refused before anything was created


def test_a_build_is_refused_before_start(monkeypatch, tmp_path):
    """Builds run on the host's BuildKit under runc; this mode needs a prebuilt image."""
    err, stopped = started(monkeypatch, tmp_path, lambda e: pytest.fail("must not start"), env(tmp_path, image=None))
    assert err is not None and "would build an image" in str(err) and stopped == []


def test_the_check_after_start_asks_the_trusted_daemon(monkeypatch, tmp_path):
    e = env(tmp_path)
    seen = []
    monkeypatch.setattr(g.GvisorEnvironment, "_trusted_docker_client_environment", lambda self: {"PATH": "/x"})

    def run(cmd, **kw):
        seen.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, 0, "abc\n" if "ps" in cmd else "/main runsc\n", "")

    monkeypatch.setattr(g.subprocess, "run", run)
    assert e._project_runtimes() == {"/main": "runsc"}
    assert seen == [{"PATH": "/x"}, {"PATH": "/x"}]


def test_compose_up_never_builds():
    """#139 review: a build: key on a sidecar, or pull_policy: build, would build under runc during up."""
    rt = g.GvisorEnvironment.runtime()
    assert rt.up_args[-1] == "--no-build" and rt.up_args[:2] == ["--detach", "--wait"]
    assert rt.engine == ("docker",) and rt.compose == ("docker", "compose")
