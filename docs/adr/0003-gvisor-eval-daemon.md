# ADR-0003: Live trials run under gVisor on a dedicated Docker daemon

Date: 2026-11-08 · Status: accepted (Enes, 2026-11-07, by direct message) · Not yet installed or tested

## Context
SkillEvaluator's Docker mode gives each trial a fresh container and uses the same image for both arms. It hands the
key over by stdin and redacts it from logs. But it uses the default runc runtime on a public network, the agent sees
the real key inside the container, and the skill side cannot set a network policy. The VPS has no `/dev/kvm`, which
rules out Kata, Firecracker and self-hosted E2B/Daytona. Managed sandboxes (E2B, about $0.12–0.25 per skill,
estimated) remain an option, but cost money per run.

## Decision
- A second dockerd, `docker-evals`, with its own socket (`/run/docker-evals.sock`, group `docker-evals`), its own
  data root, and `runsc` (gVisor, systrap platform, no KVM) as its default runtime. systemd caps it at 2 CPU, 3 GB of
  memory and 1024 tasks.
- The daemon's addresses stay inside `172.31.240.0/20`: trial networks come from its lower half (a /28 each) and
  the default bridge sits in the upper half. An egress firewall on the whole /20 allows the LLM gateway only, and
  denies the host, the LAN and the internet.
- Per trial: CPU, memory and wall-clock caps, no host mounts, no Docker socket, removed after the run. Live runs go
  one at a time, only for approved skills, under a daily cap.
- Drafts are in `deploy/gvisor/` and are unreviewed. Step 0 is a SkillEvaluator doctor run plus one run of the
  bundled sample skill.

## Consequences
- An escape needs a gVisor bug and then a host-kernel bug reachable from gVisor's small set of host calls.
- Overhead is about 20–50 MB per sandbox, near-native compute, and 10–50% slower file-heavy work.
- Unverified until step 0: two dockerd instances sharing iptables' `DOCKER-USER` chain, `userns-remap` with runsc,
  Harbor's compose features under gVisor, and DNS for trials.
- Moving to E2B or a second VM later changes the worker's `--env-mode` or `DOCKER_HOST`, not the design.

**Amendment (2026-11-12, #123):**
- Trials may reach two host ports on `br-evals` (172.31.248.1): the model gateway, and an egress proxy that allows
  HTTPS tunnels and plain HTTP GET and HEAD to an allowlist of download hostnames, only when every address the name
  resolves to is public. The proxy serves trial addresses only. Harbor installs the agent inside every trial at setup (system packages, Node through nvm, then npm),
  and has no hook to lock egress down after the install. Decided by Enes, 2026-11-12, by direct message.
- Trials get no DNS. The proxy resolves names itself.
- Accepted risk: skill code can reach the allowlisted hosts too, and upload to them. The proxy can't see methods
  inside a tunnel, so an `npm publish` to registry.npmjs.org, or a write to any allowlisted host that accepts one,
  is an outbound data channel. Through the CDNs in front of these hosts, a mismatched SNI or Host might also reach
  other sites on the same edge. The CDNs say they block domain fronting; that is not verified. A trial holds no
  secret, only a per-run gateway token, and the skills are public.
- The eval daemon uses its own containerd namespaces, and `firewall.sh` runs before every start of the daemon.

**Amendment (2026-11-12, #137): the second daemon is withdrawn.**
- On the shared host, starting `docker-evals` took the main daemon's networks down twice. With iptables on, it rebuilt the
  shared forwarding chains with only `br-evals` in them. With iptables off (#136), it deleted `docker0`. Neither was an
  address overlap. Two dockerds in one network namespace aren't safe, whatever the config.
- Decided by Enes, 2026-11-12, by direct message, at the request of the host's operator: no second dockerd on the
  shared host. `install.sh` and `preflight.sh` refuse whenever another dockerd is running. Every host change is
  announced before it runs.
- gVisor stays the isolation layer. Where it runs (proposed: runsc as an extra runtime on the main daemon, selected
  per trial through a SkillEvaluator `--env-mode`) is decided in #137 and recorded in a further amendment.

**Amendment (2026-11-12, #137): gVisor on the main daemon.**
- `runsc` is registered as an extra runtime on the host's main Docker daemon (`runsc install -- --platform=systrap`,
  then `systemctl reload docker`, with no restart). The default runtime stays runc. Enes ran it in an announced window
  on 2026-11-12. All existing containers stayed up, and the iptables diff was empty.
- Trials select it through `--env-mode gvisor` (`gvisor_env/`), a SkillEvaluator entry-point backend. It is
  SkillEvaluator's hardened Kata class with runtime `runsc` and resolver 127.0.0.1. Before start it refuses a trial
  with any compose service missing from the runsc overlay (a local `include:` is not covered by Harbor's overlay),
  and any trial that would build an image (builds run under runc on the host). `compose up` always gets
  `--no-build`. After start it checks that every
  container of the trial reports runtime `runsc`, and tears the trial down otherwise. That check is detection after
  start; prevention is the check before start and the socket proxy.
- Still to come before any live run, each in its own PR: a socket proxy between Harbor and the daemon, which refuses
  non-runsc, privileged, host-mounting and build calls; a boot unit for `firewall.sh`; and the gateway and proxy on a
  dedicated address. Until then `pipeline/tier3.py` refuses to start.

**Amendment (2026-11-13, #137): trial images are built by the socket proxy.**
- Agreed by the host's operator, relayed by Enes on 2026-11-13. SkillEvaluator builds an image per trial, because the
  skill's files are copied into it. So instead of refusing every build, the trial sends its build context to the
  proxy's `POST /_evalskills/build`, and the proxy writes the build itself (`gateway/docker_build.py`). The pass-through
  `/build`, `/session` and `/grpc` stay refused: Compose v2 builds through BuildKit's session, which a `/build` check
  would never see.
- Only an allowlisted Dockerfile is built. `deploy/gvisor/build/allowed-dockerfiles.txt` maps the sha256 of each
  Dockerfile that the pinned SkillEvaluator generates to a committed final Dockerfile: `FROM` the trial base image,
  then the template's fixed steps. The proxy adds that file under its own name and never rewrites anything. For the
  10 pilot skills there are exactly 4 originals, one per task shape, with nothing skill-specific in them. A custom
  environment, or a skill's `requirements.txt` or `apt-packages.txt`, gives another sha256 and is unsupported.
- The template's apt and pip installs live in the trial base image (`deploy/gvisor/build/base/`, pip pinned by hash).
  It's built off-host and loaded by image ID, which the proxy checks before every build. Trial builds run with
  `networkmode=none` and `pull=0`.
- **Docker upgrades must be checked against this.** The proxy uses Docker's legacy builder (`version=1`). If a Docker
  release removes it, builds fail with an error, and there is no BuildKit fallback. Before upgrading Docker on the
  host, check that `version=1` builds still work. If they don't, this amendment needs a replacement.
- Build `RUN` steps still execute under the daemon's default runtime (runc). Those are only the template's fixed
  commands. The skill's files are present but never run. Accepted on #137.
- The SkillEvaluator repin procedure now includes regenerating the allowlist (`scripts/gvisor_dockerfiles.py`) and
  reviewing the diff.

**Amendment (2026-11-13, #137): who runs a trial.**
- `pipeline/tier3.py` runs only `--env-mode gvisor` (`gvisor_env/`) with `DOCKER_HOST` set to the socket proxy. It
  refuses to start when the proxy's `GET /_evalskills/firewall` doesn't report the trial firewall in place.
- The pilot stays the operator, because it issues and revokes gateway tokens on the main daemon. Only the
  SkillEvaluator process runs as the trial user (`TIER3_TRIAL_USER`, default `evals`). That user's only Docker access
  is the proxy socket's group, and the runner refuses a trial user that could open Docker's own socket. It gets the
  token in its environment, never the token file.
- SkillEvaluator forwards no `DOCKER_*` variable from a custom backend to Harbor. So the proxy's path travels as
  `EVALSKILLS_DOCKER_SOCKET`, and GvisorEnvironment sets `DOCKER_HOST` from it in Harbor's process before any Docker
  call. Unset, it defaults to `/run/evalskills/docker.sock`, never Docker's default socket.
- Each trial gets a fresh folder under `TIER3_RUN_ROOT` (`/srv/evals/run`, the proxy's `bind_root`, owned by the
  trial user). The skill copy, SkillEvaluator's results (Harbor's trial folders, which become binds) and `TMPDIR` all
  live in that folder. When the trial ends, the operator first stops everything of it: the child runs in its own
  session, which is killed, then any process left under the trial uid, and the pilot stops if one survives. Then it
  removes our containers and networks through the proxy. Then it copies the report into a fresh `reports/` folder
  as root: only folders and the bytes of regular files, as 0755/0644, never through a link and never opening a FIFO.
  Everything else is listed in `skipped.json`. Last, it removes the trial folder. The log goes to a root file,
  never into that folder. So a link a skill plants can reach only that trial's own files.
- SkillEvaluator runs from its synced venv, with `evalskills_gvisor` installed into it once at setup. It doesn't run
  through `uv run`, which would have the trial user write to the evaluator's environment.
- A trial whose Dockerfile the proxy refuses is recorded as `unsupported` (needs a custom environment or
  dependencies), never `failed` and never scored. The evidence is a file that GvisorEnvironment writes
  (`EVALSKILLS_UNSUPPORTED_FILE`), in a folder no container is bound to, not the log, which a skill can fill.
