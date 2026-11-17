"""#137: SkillEvaluator --env-mode gvisor. Needs SkillEvaluator and Harbor, so it runs where they are installed:

  cd $SKILLEVALUATOR_DIR && uv run -q --python 3.13 --extra tier3 --with pytest --with-editable <repo>/gvisor_env \\
    python -m pytest <repo>/tests/test_gvisor_env_mode.py

Elsewhere (CI, the plain test run) it is skipped."""

import asyncio
import io
import json
import os
import subprocess
import sys
import tarfile
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


@pytest.fixture(autouse=True)
def _restore_docker_env(monkeypatch):
    """GvisorEnvironment sets DOCKER_HOST in its process; give each test the original back."""
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "default")
    monkeypatch.delenv(g.SOCKET_ENV, raising=False)


def test_harbor_gets_the_proxy_socket_and_the_unsupported_mark(monkeypatch):
    """SkillEvaluator's own lookup: Harbor's environment is its base set plus the backend's host_env_vars, and the
    backend loads (a forbidden name, such as DOCKER_HOST, makes the lookup return nothing)."""
    from skillevaluator.tier3.harbor import runner

    assert runner._custom_backend("gvisor") is g.SPEC
    names = runner._env_mode_host_env_vars("gvisor")
    assert {g.SOCKET_ENV, g.UNSUPPORTED_FILE_ENV} <= names
    monkeypatch.setenv(g.SOCKET_ENV, "/run/evalskills/docker.sock")
    monkeypatch.setenv(g.UNSUPPORTED_FILE_ENV, "/srv/evals/run/t3-x/marks/unsupported")
    got = runner._selected_host_environment(names, os.environ)
    assert got[g.SOCKET_ENV] == "/run/evalskills/docker.sock"
    assert got[g.UNSUPPORTED_FILE_ENV] == "/srv/evals/run/t3-x/marks/unsupported"


def test_the_environment_points_docker_at_the_proxy_never_the_default(tmp_path, monkeypatch):
    e = env(tmp_path)
    trusted = e._trusted_docker_client_environment()
    assert trusted["DOCKER_HOST"] == "unix://" + g.DEFAULT_SOCKET and "DOCKER_CONTEXT" not in trusted
    monkeypatch.setenv(g.SOCKET_ENV, "/srv/x/proxy.sock")
    assert env(tmp_path)._trusted_docker_client_environment()["DOCKER_HOST"] == "unix:///srv/x/proxy.sock"
    for bad in ("relative.sock", "/var/run/docker.sock", "/run/docker.sock"):
        monkeypatch.setenv(g.SOCKET_ENV, bad)
        with pytest.raises(RuntimeError, match="socket proxy"):
            env(tmp_path)


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


def stub_docker(tmp_path, monkeypatch, exit_code=0, stderr=""):
    """A fake `docker` first on PATH. It logs the DOCKER_HOST and DOCKER_CONTEXT it was called with, and answers
    `info` with runc and runsc registered."""
    bin_dir, log = tmp_path / "bin", tmp_path / "docker.log"
    bin_dir.mkdir()
    script = bin_dir / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "host=${{DOCKER_HOST:-}} context=${{DOCKER_CONTEXT:-}} args=$*" >> {log}\n'
        f'[ {exit_code} = 0 ] || {{ echo "{stderr}" >&2; exit {exit_code}; }}\n'
        'case "$*" in *Runtimes*) echo \'{"runc": {}, "runsc": {}}\' ;; esac\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def test_preflight_asks_the_proxy_even_before_any_environment_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("DOCKER_HOST")  # as Harbor's process starts: SkillEvaluator forwards no DOCKER_* variable
    log = stub_docker(tmp_path, monkeypatch)
    g.GvisorEnvironment.preflight()  # Harbor calls it on the class, before any instance (#185)
    calls = log.read_text().splitlines()
    assert any("Runtimes" in c for c in calls) and any(c.endswith("args=info") for c in calls), calls
    assert all(c.startswith(f"host=unix://{g.DEFAULT_SOCKET} context= ") for c in calls), calls


def test_a_failed_docker_info_names_its_error_not_a_missing_runsc(tmp_path, monkeypatch):
    stub_docker(tmp_path, monkeypatch, exit_code=1, stderr="permission denied while trying to connect")
    with pytest.raises(SystemExit, match="exited 1: permission denied") as e:
        g.GvisorEnvironment.preflight()
    assert "not registered" not in str(e.value)


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


def test_a_build_goes_through_the_proxy_and_becomes_the_prebuilt_image(monkeypatch, tmp_path):
    """#137 PR B: Compose never builds; the proxy builds the allowlisted Dockerfile and returns a tag."""
    e = env(tmp_path, image=None)
    original = e.task_env_config
    seen = {}

    async def harbor_start(self, force_build):
        seen["force_build"], seen["image"] = force_build, self._env_vars.prebuilt_image_name

    monkeypatch.setattr(SkillEvaluatorSecureDockerEnvironment, "start", harbor_start)
    monkeypatch.setattr(g.GvisorEnvironment, "_build_through_proxy", lambda self: "evalskills-trial:abc")
    monkeypatch.setattr(g.GvisorEnvironment, "_project_runtimes", lambda self: {"/main": "runsc"})
    asyncio.run(e.start(force_build=True))
    assert seen == {"force_build": False, "image": "evalskills-trial:abc"}
    assert e.task_env_config.docker_image == "evalskills-trial:abc"
    assert original.docker_image is None  # the task's own config is copied, not changed


def test_a_refused_build_is_reported_as_unsupported(monkeypatch, tmp_path):
    e = env(tmp_path, image=None)

    class Resp:
        status = 403

        def read(self, n):
            return b'{"message": "the Dockerfile is not on the allowlist"}'

    class Conn:
        def __init__(self, *a, **k):
            pass

        def request(self, method, target, body, headers):
            assert target == g.BUILD_ROUTE and set(headers) == {"Host", "Content-Type", "Content-Length"}
            names = tarfile.open(fileobj=io.BytesIO(body)).getnames()
            assert names == ["Dockerfile"]

        def getresponse(self):
            return Resp()

        def close(self):
            pass

    monkeypatch.setattr(g, "_Unix", Conn)
    monkeypatch.setattr(g.GvisorEnvironment, "_proxy_socket", lambda self: "/run/evalskills/docker.sock")
    monkeypatch.setenv(g.UNSUPPORTED_FILE_ENV, str(tmp_path / "unsupported"))
    with pytest.raises(RuntimeError, match="unsupported environment"):
        e._build_through_proxy()
    assert "not on the allowlist" in (tmp_path / "unsupported").read_text()  # the runner's evidence, not the log


def test_a_real_build_goes_from_the_environment_to_the_proxy_handler(monkeypatch, tmp_path):
    """No stubbed request(): GvisorEnvironment's own HTTP client against the real proxy Handler (#142 review)."""
    sys.path.insert(0, str(Path(__file__).parent))
    import test_docker_build as tdb

    e = env(tmp_path, image=None)
    (e.environment_dir / "Dockerfile").write_bytes(tdb.ORIGINAL)
    (e.environment_dir / "skills").mkdir()
    (e.environment_dir / "skills" / "SKILL.md").write_text("hi")
    with tdb.running_proxy(tmp_path / "proxy") as p:
        monkeypatch.setenv(g.SOCKET_ENV, p["sock"])  # the real path: the environment's own lookup
        tag = e._build_through_proxy()
        [(_, body)] = p["state"]["builds"]
    assert tag.startswith(tdb.db.TAG_REPO + ":")
    with tarfile.open(fileobj=io.BytesIO(body)) as t:
        assert t.extractfile(tdb.db.FINAL_NAME).read() == tdb.FINAL
        assert t.extractfile("skills/SKILL.md").read() == b"hi"


def test_the_context_tar_keeps_links_as_links(tmp_path):
    d = tmp_path / "ctx"
    (d / "skills").mkdir(parents=True)
    (d / "Dockerfile").write_text("FROM scratch\n")
    (d / "skills" / "l").symlink_to("/etc/shadow")
    with tarfile.open(fileobj=io.BytesIO(g.context_tar(d))) as t:
        m = t.getmember("skills/l")
        assert m.issym() and m.linkname == "/etc/shadow"


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


def test_compose_up_never_builds_or_pulls():
    """#139 review: a build: key on a sidecar, or pull_policy: build, would build under runc during up. #203: Compose
    asked the registry about the proxy-built image before up, the proxy refused it, and every trial failed."""
    rt = g.GvisorEnvironment.runtime()
    assert rt.up_args == ["--detach", "--wait", "--no-build", "--pull", "never"]
    assert rt.engine == ("docker",) and rt.compose == ("docker", "compose")
