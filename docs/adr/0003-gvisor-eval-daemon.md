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
