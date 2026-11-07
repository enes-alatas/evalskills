# Eval sandbox on the shared VPS: dedicated Docker daemon + gVisor (DRAFT, review before running)

Goal: live Tier 3 trials run under gVisor (runsc, systrap platform, so no /dev/kvm is needed). They run on a
**separate** Docker daemon, so the main daemon and its apps are untouched.

| Item | Value |
| --- | --- |
| Socket | `/run/docker-evals.sock` (group `docker-evals`; only the orchestrator gets it) |
| Data root | `/var/lib/docker-evals` |
| Default runtime | `runsc` |
| Bridge | `br-evals`, 172.31.250.0/24 (must not overlap existing networks) |
| Egress | only the model gateway (see `firewall.sh`) |

Steps (as root): `install.sh`, review `daemon-evals.json` and the unit, `systemctl enable --now docker-evals`,
then run `firewall.sh`. Step 0 test: `DOCKER_HOST=unix:///run/docker-evals.sock docker run --rm hello-world`, then
`dmesg`/`docker info` show runtime runsc. After that, a SkillEvaluator doctor plus one bundled-skill Tier 3 run.

Undo: `systemctl disable --now docker-evals && rm -rf /var/lib/docker-evals /etc/docker-evals`.

Known uncertainties to check in step 0:
- Two dockerd instances share the host's iptables (both use the DOCKER-USER chain). Check that the main daemon's rules
  survive restarts of both.
- `userns-remap` together with runsc, and Harbor's compose features under gVisor, are unverified.
- DNS is blocked by `firewall.sh`. The gateway must be addressed by IP, or allow the resolver explicitly.
