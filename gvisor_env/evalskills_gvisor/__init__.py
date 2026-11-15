"""SkillEvaluator ``--env-mode gvisor`` (#137): trials on the host's main Docker daemon, every container under runsc.

Harbor's Kata backend already does what we need, with one runtime instead of another. It is SkillEvaluator's
hardened Docker backend plus a final compose overlay that sets ``runtime:`` on every task service, so a task file
can't opt out, and that binds a literal resolv.conf. We reuse SkillEvaluator's hardened Kata class and change four
things:
- the runtime is ``runsc``, and the DNS file names 127.0.0.1, so trials get no resolver. Neither can be set by an
  operator (no environment kwargs are accepted);
- preflight checks that runsc is registered with Docker, and not for /dev/kvm (gVisor's systrap platform needs none);
- before start, every service in SkillEvaluator's hardened compose model must be in the runsc overlay. Harbor's overlay
  doesn't follow a local ``include:``, so an included service would otherwise run under runc. And Compose never
  builds: ``compose up`` always gets ``--no-build`` (a ``build:`` key on a sidecar, or ``pull_policy: build``, would
  build on the host's BuildKit). Instead, when the task has a Dockerfile, the environment sends its build context to
  the Docker socket proxy's ``POST /_evalskills/build``. The proxy builds the committed Dockerfile that matches the
  template's (legacy builder, no network, the pinned base image, see gateway/docker_build.py) and returns a tag,
  which then becomes the trial's prebuilt image. A Dockerfile that isn't the template's (a custom environment, or a
  skill's requirements.txt or apt-packages.txt) is refused there, and the trial fails as unsupported;
- after start, every container of the trial's compose project must report ``HostConfig.Runtime == runsc``. If any
  doesn't, or none can be found, the trial is torn down and the start fails. This *detects* a non-runsc container
  after the fact; it doesn't prevent one. Prevention is the check before start, plus the socket proxy (#137).
Kata's other limits carry over: no egress-control sidecar, so non-public network policies are refused. Egress is the
trial network's job: the socket proxy makes it internal with no host address, and attaches only the gateway and the
egress proxy to it (gateway/docker_proxy.py).

Harbor labels this environment type "kata"; SkillEvaluator's reports carry the env mode "gvisor".
"""

from __future__ import annotations

import asyncio
import http.client
import io
import json
import os
import socket
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harbor.environments.definition import should_use_prebuilt_docker_image
from harbor.environments.docker.docker import _sanitize_docker_compose_project_name
from harbor.environments.docker.runtime import DOCKER_RUNTIME, ContainerRuntime
from skillevaluator.tier3.harbor.secure_docker_environment import SkillEvaluatorSecureKataEnvironment
from skillevaluator.tier3_environments import EnvironmentBackendSpec

RUNTIME = "runsc"
NO_DNS = ("127.0.0.1",)
BUILD_ROUTE = "/_evalskills/build"
UNSUPPORTED_BUILD = "gvisor env mode: unsupported environment"
UNSUPPORTED_FILE_ENV = "EVALSKILLS_UNSUPPORTED_FILE"  # pipeline/tier3.py sets it
# SkillEvaluator lets a backend forward no DOCKER_* variable to Harbor, so the proxy's socket comes under our own
# name and becomes DOCKER_HOST inside Harbor's process (#143 review). Docker's default socket is never used.
SOCKET_ENV = "EVALSKILLS_DOCKER_SOCKET"
DEFAULT_SOCKET = "/run/evalskills/docker.sock"


def proxy_socket() -> str:
    path = os.environ.get(SOCKET_ENV) or DEFAULT_SOCKET
    if not os.path.isabs(path) or path.endswith("/docker.sock") and path.startswith(("/var/run/", "/run/docker")):
        raise RuntimeError(f"gvisor env mode: {SOCKET_ENV} must be the socket proxy's absolute path")
    return path


def use_proxy() -> None:
    """Point every Docker client in this process at the socket proxy: the trusted client, Compose, and Harbor's own
    class-level checks. Harbor runs preflight() as a class method before any environment exists (#185), so this is
    called there as well as in __init__."""
    os.environ["DOCKER_HOST"] = "unix://" + proxy_socket()
    os.environ.pop("DOCKER_CONTEXT", None)


class _Unix(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def context_tar(environment_dir) -> bytes:
    """The task's build context as an uncompressed tar. Links are stored as links, never followed."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as t:
        for path in sorted(environment_dir.rglob("*")):
            t.add(path, arcname=path.relative_to(environment_dir).as_posix(), recursive=False)
    return out.getvalue()


@dataclass(frozen=True)
class _NoBuildDockerRuntime(ContainerRuntime):
    """Docker, with ``compose up`` never building: a build runs under runc on the host's BuildKit."""

    @property
    def up_args(self) -> list[str]:
        return [*super().up_args, "--no-build"]


NO_BUILD_DOCKER = _NoBuildDockerRuntime(engine=DOCKER_RUNTIME.engine, compose=DOCKER_RUNTIME.compose)


class GvisorEnvironment(SkillEvaluatorSecureKataEnvironment):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["kata_runtime"] = RUNTIME
        kwargs["kata_dns"] = NO_DNS
        use_proxy()  # before anything talks to Docker
        super().__init__(*args, **kwargs)

    @classmethod
    def runtime(cls) -> ContainerRuntime:
        return NO_BUILD_DOCKER

    @staticmethod
    def _host_has_kvm() -> bool:
        return True  # gVisor's systrap platform runs without hardware virtualization

    @classmethod
    def _docker_runtimes(cls) -> list[str]:
        """The runtimes the daemon behind the proxy has registered. A failed `docker info` raises with its error, so
        a refused socket or a timeout never reads as "runsc not registered"."""
        env = {k: v for k, v in os.environ.items() if k != "DOCKER_CONTEXT"}
        env["DOCKER_HOST"] = "unix://" + proxy_socket()
        try:
            p = subprocess.run(
                cls._engine_cmd("info", "--format", "{{json .Runtimes}}"), capture_output=True, timeout=10, env=env
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            raise SystemExit(f"gvisor env mode: `docker info` through the socket proxy failed: {e}") from e
        if p.returncode != 0:
            err = p.stderr.decode(errors="replace").strip()[-500:]
            raise SystemExit(f"gvisor env mode: `docker info` through the socket proxy exited {p.returncode}: {err}")
        try:
            runtimes = json.loads(p.stdout or b"{}")
        except ValueError as e:
            raise SystemExit("gvisor env mode: `docker info` through the socket proxy printed no runtimes") from e
        return sorted(runtimes) if isinstance(runtimes, dict) else []

    @classmethod
    def _registered_kata_runtimes(cls) -> list[str]:
        return [RUNTIME] if RUNTIME in cls._docker_runtimes() else []

    @classmethod
    def preflight(cls) -> None:
        use_proxy()  # Harbor's Docker check, in super().preflight(), runs `docker info` with this process's env
        if RUNTIME not in cls._docker_runtimes():
            raise SystemExit(
                "runsc is not registered with Docker. Install gVisor and run `runsc install -- --platform=systrap`, "
                "then `systemctl reload docker` (evalskills #137)."
            )
        super().preflight()

    def _project_runtimes(self) -> dict[str, str]:
        """Container name → runtime for every container of this trial's compose project."""
        project = _sanitize_docker_compose_project_name(self.session_id)
        docker_env = self._trusted_docker_client_environment()  # the same daemon and context as the compose runner
        ids = subprocess.run(
            self._engine_cmd("ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            env=docker_env,
        ).stdout.split()
        if not ids:
            return {}
        out = subprocess.run(
            self._engine_cmd("inspect", "--format", "{{.Name}} {{.HostConfig.Runtime}}", *ids),
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            env=docker_env,
        ).stdout
        return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)

    def _refuse_before_start(self) -> None:
        """Prevention, before anything is built or created: every declared service is pinned to runsc."""
        declared = set(self._compose_model_metadata()[1])
        pinned = set(json.loads(self._ensure_runtime_compose_file().read_text())["services"])
        if missing := sorted(declared - pinned):
            raise RuntimeError(f"gvisor env mode refused the trial: services not pinned to runsc: {', '.join(missing)}")

    def _proxy_socket(self) -> str:
        return proxy_socket()

    def _build_through_proxy(self) -> str:
        """Send the build context to the proxy, which writes and runs the build itself. → the image tag."""
        body = context_tar(self.environment_dir)
        conn = _Unix(self._proxy_socket(), timeout=900)
        try:
            conn.request(
                "POST",
                BUILD_ROUTE,
                body=body,
                headers={"Host": "docker", "Content-Type": "application/x-tar", "Content-Length": str(len(body))},
            )
            resp = conn.getresponse()
            doc = json.loads(resp.read(1024 * 1024) or b"{}")
        except (OSError, ValueError) as e:
            raise RuntimeError(f"gvisor env mode: the build request failed ({type(e).__name__})") from e
        finally:
            conn.close()
        if resp.status == 403:
            if mark := os.environ.get(UNSUPPORTED_FILE_ENV):  # the runner's evidence; a skill can't write it
                Path(mark).write_text(json.dumps({"message": str(doc.get("message", "refused"))[:500]}))
            raise RuntimeError(f"{UNSUPPORTED_BUILD}: {doc.get('message', 'refused')}")
        if resp.status != 200 or not isinstance(doc.get("tag"), str):
            raise RuntimeError(f"gvisor env mode: the build failed ({resp.status}): {doc.get('message', '')}")
        return doc["tag"]

    async def start(self, force_build: bool) -> None:
        self._refuse_before_start()
        if not should_use_prebuilt_docker_image(
            self.environment_dir, docker_image=self.task_env_config.docker_image, force_build=force_build
        ):
            tag = await asyncio.to_thread(self._build_through_proxy)
            # a copy: the caller may share this config with other trials (#142 review)
            self.task_env_config = self.task_env_config.model_copy(update={"docker_image": tag})
            self._env_vars.prebuilt_image_name = tag
        await super().start(False)
        try:
            runtimes = await asyncio.to_thread(self._project_runtimes)
        except (subprocess.SubprocessError, OSError) as e:
            runtimes, why = {}, f"could not inspect the trial's containers ({type(e).__name__})"
        else:
            why = "no containers found for the trial's compose project"
        wrong = {name: rt for name, rt in runtimes.items() if rt != RUNTIME}
        if runtimes and not wrong:
            return
        if wrong:
            why = "not under runsc: " + ", ".join(f"{n} ({rt or 'default'})" for n, rt in sorted(wrong.items()))
        await self.stop(delete=True)
        raise RuntimeError(f"gvisor env mode refused the trial: {why}")


SPEC = EnvironmentBackendSpec(
    import_path="evalskills_gvisor:GvisorEnvironment",
    supports_docker_compose=True,
    # SkillEvaluator gives Harbor only its base variables plus these (#143 review); pipeline/tier3.py sets both and
    # refuses a trial user that could open Docker's own socket.
    host_env_vars=frozenset({SOCKET_ENV, UNSUPPORTED_FILE_ENV}),
    host_path_env_vars=frozenset({SOCKET_ENV, UNSUPPORTED_FILE_ENV}),
    install_hint="install evalskills' gvisor_env package next to SkillEvaluator: uv run --with-editable ./gvisor_env",
)
