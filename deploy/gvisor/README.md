# Eval sandbox on the shared VPS: dedicated Docker daemon + gVisor (DRAFT, review before running)

Goal: live Tier 3 trials run under gVisor (runsc, systrap platform, so no /dev/kvm is needed). They run on a
**separate** Docker daemon, so the main daemon and its apps are untouched.

| Item | Value |
| --- | --- |
| Socket | `/run/docker-evals.sock` (group `docker-evals`; only the orchestrator gets it) |
| Data root | `/var/lib/docker-evals` |
| Default runtime | `runsc` |
| Default bridge | `br-evals`, 172.31.248.0/24 |
| Trial networks | pool 172.31.240.0/21, one /28 per network (Harbor creates one per trial) |
| Firewall scope | 172.31.240.0/20, which covers both of the above; it must not overlap the host's other networks |
| containerd namespaces | `evals` and `evals-plugins`, so this daemon never sees the main daemon's `moby` containers |
| Egress | the model gateway and the egress proxy only (ADR-0003, amended 2026-11-12), when their addresses are set in `firewall.env`; deny-all otherwise |
| DNS | none for trials: the gateway is addressed by IP, and the proxy resolves names itself |

Steps (as root), in order:
1. `sh install.sh`, then review `/etc/docker-evals/daemon.json` and `/etc/docker-evals/firewall.env`. Leave both
   addresses in `firewall.env` unset for now.
2. `systemctl enable --now docker-evals`. Its `ExecStartPre` runs `firewall.sh` before every start, so the daemon
   never runs without the rules, and a reboot restores them. Nothing is saved with `iptables-save`.
3. Deny-all test: `DOCKER_HOST=unix:///run/docker-evals.sock docker run --rm hello-world` works (the image is
   pulled by the daemon, not the container), `docker info` shows runtime runsc, and a container cannot reach the
   internet, the host or the main Docker networks.
4. Once the gateway and the egress proxy exist (#121, PR A): set their addresses in `firewall.env`, then
   `systemctl restart docker-evals`.
5. A SkillEvaluator doctor, then one bundled-skill Tier 3 run.

Undo: `systemctl disable --now docker-evals && rm -rf /var/lib/docker-evals /etc/docker-evals`, then remove the
rules: `iptables -D INPUT -s 172.31.240.0/20 -j EVALS-INPUT`, `iptables -D FORWARD -s 172.31.240.0/20 -j EVALS-EGRESS`, `iptables -D DOCKER-USER -s 172.31.240.0/20 -j EVALS-EGRESS`,
and `iptables -F` and `iptables -X` on both chains.

Known uncertainties to check in step 0:
- Two dockerd instances share the host's iptables. **This broke the host on 2026-11-12:** starting the eval daemon
  cut egress for containers on the main daemon (its own address ranges did not overlap, 172.17-26.x). The eval
  daemon now runs with `"iptables": false`, so it writes no rules at all; `firewall.sh` is the only thing that adds
  rules for its range, and trials need no NAT (the gateway and proxy are on host addresses). `preflight.sh` also
  refuses to start it if any host route overlaps 172.31.240.0/20. Before enabling it again, snapshot the rules
  (`iptables-save > /root/before.rules`), start it, and diff (`iptables-save | diff /root/before.rules -`): only the
  EVALS-* chains and their three jumps (INPUT, FORWARD, DOCKER-USER) may appear. Isolation between trials comes
  from EVALS-EGRESS alone: `icc` is enforced by Docker's iptables rules, so the config no longer sets it.
- `userns-remap` together with runsc, and Harbor's compose features under gVisor, are unverified.
- No DNS: on a compose network created by the eval daemon, `getent hosts example.com` (or `nslookup`) must fail.
  Docker's embedded resolver (127.0.0.11) forwards from dockerd, possibly from the host's namespace, outside the
  source-address filter. If names resolve, trials have DNS (and a DNS channel out) whatever the firewall says, and
  the resolver must be disabled before any trial runs.
- No IPv6: a trial has no global IPv6 address. `firewall.sh` is IPv4 only, so an IPv6-enabled trial network would
  leave unfiltered.
- `ctr namespaces ls` lists `evals` next to `moby`, and the main daemon's `docker ps` is unchanged.
- Two containers on one trial network can still reach each other. br_netfilter sends bridged traffic through
  FORWARD, so EVALS-EGRESS may drop it. If a multi-container trial fails this, add a same-bridge allow
  (`-m physdev --physdev-is-bridged`) to firewall.sh.
- The INPUT, FORWARD and DOCKER-USER jumps stay at position 1 after a ufw reload. If not, run `firewall.sh` again; it removes
  its old jumps before inserting new ones.
- Agent installs: Harbor installs the agent inside every trial at setup (system packages, Node through nvm, then
  `npm i -g opencode-ai`), so trials need the egress proxy. Its hostname allowlist (package mirror, nodejs.org,
  raw.githubusercontent.com, registry.npmjs.org) comes with #121's PR A and is unverified (#123).
