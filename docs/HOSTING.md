# Hosting requirements

The design is in `docs/ARCHITECTURE.md` and its ADRs. This file lists what the host needs.

## Static release (zone A only, no skill code executed)
- A Linux host with Docker and a TLS reverse proxy (Caddy, as a host daemon) that sets `X-Forwarded-For` and
  proxies to `127.0.0.1:8088`. 2 vCPU, 4 GB RAM and 40 GB disk are enough.
- Docker Engine 28 or later. Older engines let hosts on the same L2 network route straight to the container's
  bridge address, which bypasses the `127.0.0.1` port binding. Check with
  `docker version --format '{{.Server.Version}}'`.
- Egress: github.com, objects.githubusercontent.com, pypi.org, files.pythonhosted.org.
- No provider credentials on the app container.
- Guards already in the app: a 300 MB repo cap, shallow clones with symlinks off, scans on a snapshot copy, a 900 s
  timeout, submission and lookup rate limits, and a queue cap.

## Live release (adds zones B and C)
- gVisor (`runsc`) registered as an extra runtime on the main Docker daemon (Docker Engine 28 or later), as in
  ADR-0003 (amended 2026-11-14). No `/dev/kvm` and no second daemon are needed.
- The host steps in `deploy/gvisor/host/`, each announced and run by the host's operator. None of them changes
  iptables: trials are isolated by internal networks with no address on the host.
- Two free ranges: 172.31.240.0/21 for trial networks and 172.31.248.0/24 for the services' egress network.
- About 2 CPU and 3 GB RAM for trials, on top of the app.
- A model-provider key on the host for the gateway (ADR-0004). Egress from the gateway goes to the provider's API
  only.
- The model gateway and the trial egress proxy (`deploy/live/`), on the main daemon on their own egress network,
  attached by the socket proxy to each trial network. They publish nothing. The proxy reaches an allowlist of
  download hosts (ADR-0003, amended 2026-11-12 and 2026-11-14).

## Managed sandbox alternative (estimate, not measured)
E2B or Daytona: 2 vCPU and 4 GiB cost about $0.17/h. about 4 cases × 2 arms + 1 preflight ≈ 9 sandboxes × 5–10 min
≈ $0.12–0.25 per skill, so a $100 credit covers about 400–800 skill evaluations. Model calls are billed separately.
Rates came from E2B billing docs via secondary sources (2026-11-07).
