"""SkillEvaluator ``--env-mode gvisor`` (#137): trials on the host's main Docker daemon, every container under runsc.

Harbor's Kata backend already does what we need, with one runtime instead of another. It is SkillEvaluator's
hardened Docker backend plus a final compose overlay that sets ``runtime:`` on every task service, so a task file
can't opt out, and that binds a literal resolv.conf. We reuse SkillEvaluator's hardened Kata class and change four
things:
- the runtime is ``runsc``, and the DNS file names 127.0.0.1, so trials get no resolver. Neither can be set by an
  operator (no environment kwargs are accepted);
- preflight checks that runsc is registered with Docker, and not for /dev/kvm (gVisor's systrap platform needs none);
- before start, every service in SkillEvaluator's hardened compose model must be in the runsc overlay. Harbor's overlay
  doesn't follow a local ``include:``, so an included service would otherwise run under runc. And Harbor must not
  build: ``docker compose build`` runs on the host's BuildKit under runc, so a trial needs a prebuilt image, and
  ``compose up`` always gets ``--no-build`` (a ``build:`` key on a sidecar, or ``pull_policy: build``, would build);
- after start, every container of the trial's compose project must report ``HostConfig.Runtime == runsc``. If any
  doesn't, or none can be found, the trial is torn down and the start fails. This *detects* a non-runsc container
  after the fact; it doesn't prevent one. Prevention is the check before start, plus the socket proxy (#137).
Kata's other limits carry over: no egress-control sidecar, so non-public network policies are refused. Egress is the
host firewall's job (deploy/gvisor/firewall.sh).

Harbor labels this environment type "kata"; SkillEvaluator's reports carry the env mode "gvisor".
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from typing import Any

from harbor.environments.definition import should_use_prebuilt_docker_image
from harbor.environments.docker.docker import _sanitize_docker_compose_project_name
from harbor.environments.docker.runtime import DOCKER_RUNTIME, ContainerRuntime
from skillevaluator.tier3.harbor.secure_docker_environment import SkillEvaluatorSecureKataEnvironment
from skillevaluator.tier3_environments import EnvironmentBackendSpec

RUNTIME = "runsc"
NO_DNS = ("127.0.0.1",)


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
        super().__init__(*args, **kwargs)

    @classmethod
    def runtime(cls) -> ContainerRuntime:
        return NO_BUILD_DOCKER

    @staticmethod
    def _host_has_kvm() -> bool:
        return True  # gVisor's systrap platform runs without hardware virtualization

    @classmethod
    def _docker_runtimes(cls) -> list[str]:
        try:
            p = subprocess.run(
                cls._engine_cmd("info", "--format", "{{json .Runtimes}}"), capture_output=True, timeout=10
            )
            runtimes = json.loads(p.stdout or b"{}")
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return []
        return sorted(runtimes) if isinstance(runtimes, dict) else []

    @classmethod
    def _registered_kata_runtimes(cls) -> list[str]:
        return [RUNTIME] if RUNTIME in cls._docker_runtimes() else []

    @classmethod
    def preflight(cls) -> None:
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

    def _refuse_before_start(self, force_build: bool) -> None:
        """Prevention, before anything is built or created: every declared service is pinned, and nothing is built."""
        declared = set(self._compose_model_metadata()[1])
        pinned = set(json.loads(self._ensure_runtime_compose_file().read_text())["services"])
        if missing := sorted(declared - pinned):
            raise RuntimeError(f"gvisor env mode refused the trial: services not pinned to runsc: {', '.join(missing)}")
        if not should_use_prebuilt_docker_image(
            self.environment_dir, docker_image=self.task_env_config.docker_image, force_build=force_build
        ):
            raise RuntimeError(
                "gvisor env mode refused the trial: it would build an image on the host (runc); use a prebuilt image"
            )

    async def start(self, force_build: bool) -> None:
        self._refuse_before_start(force_build)
        await super().start(force_build)
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
    install_hint="install evalskills' gvisor_env package next to SkillEvaluator: uv run --with-editable ./gvisor_env",
)
