# Hosting requirements

The design is in `docs/ARCHITECTURE.md` and its ADRs. This file lists what the host needs.

## Static release (zone A only, no skill code executed)
- A Linux host with Docker and a TLS reverse proxy (Caddy) that sets `X-Forwarded-For`. 2 vCPU, 4 GB RAM and 40 GB
  disk are enough.
- Egress: github.com, objects.githubusercontent.com, pypi.org, files.pythonhosted.org.
- No provider credentials on the app container.
- Guards already in the app: a 300 MB repo cap, shallow clones with symlinks off, scans on a snapshot copy, a 600 s
  timeout, submission and lookup rate limits, and a queue cap.

## Live release (adds zones B and C)
- gVisor (`runsc`) and a second Docker daemon as in ADR-0003. No `/dev/kvm` is needed.
- About 2 CPU and 3 GB RAM reserved for the eval daemon, on top of the app.
- A model-provider key on the host for the gateway (ADR-0004). Egress from the gateway goes to the provider's API
  only.

## Managed sandbox alternative (estimate, not measured)
E2B or Daytona: 2 vCPU and 4 GiB cost about $0.17/h. about 4 cases × 2 arms + 1 preflight ≈ 9 sandboxes × 5–10 min
≈ $0.12–0.25 per skill, so a $100 credit covers about 400–800 skill evaluations. Model calls are billed separately.
Rates came from E2B billing docs via secondary sources (2026-11-07).
