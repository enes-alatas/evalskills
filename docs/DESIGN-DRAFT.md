# System design: TEMPORARY DRAFT

Status: agreed with Enes on 2026-11-07 (Telegram). This file is temporary. Once the SDLC is settled, fold it into
the permanent docs (`HOSTING.md`, `deploy/`). Diagram: `design/system.png`. Nothing below has been deployed or tested yet.

## Constraints
- One shared VPS. It also runs Enes's other apps behind the existing Caddy. It has **no /dev/kvm**, which rules out
  Kata, Firecracker and self-hosted E2B/Daytona.
- A static-only public release is fine. Live checks depend on API sponsorship and budget.
- The app must **not** run in skillator's container, because that container holds skillator's credentials.
- No container ever gets the main Docker socket.

## Zones
| Zone | What runs there | Trust | Secrets | Docker access |
| --- | --- | --- | --- | --- |
| A: public app | SPA + API (discover, submit, limits), static worker (SkillEvaluator Tier 1 scan, $0), SQLite (skills, jobs, results) and sanitized public reports | Parses untrusted input, never executes it | None | None |
| B: orchestration | Live worker (SkillEvaluator Tier 3 on shared category tasks, 1 trial at a time, daily cap, approved skills only) and the LLM gateway (holds the real key, issues per-run tokens, enforces token and $ budgets, records cost) | Trusted code | Model key (gateway only) | Eval-daemon socket only |
| C: untrusted execution | Ephemeral trial sandboxes: agent + skill scripts + grader, with-skill arm and baseline arm | Untrusted | Per-run token only | None |

## Sandbox: gVisor on a dedicated Docker daemon (Enes's choice, ref:9000)
- A second dockerd, `docker-evals`, with its own socket `/run/docker-evals.sock` (group `docker-evals`), data root
  `/var/lib/docker-evals`, and **runsc as its default runtime** (systrap platform, no KVM needed).
- systemd caps on the daemon: 2 CPU, 3 GB RAM, 1024 tasks.
- Address pool `172.31.240.0/20`, with /28 per trial network, because Harbor creates a compose network for each trial.
- Egress firewall on that pool: the LLM gateway is allowed; the host, LAN and internet are denied.
- Per trial: CPU, memory and wall-clock caps, no host mounts, no Docker socket, destroyed after the run.
- Expected overhead: about 20–50 MB RAM per sandbox, near-native compute, and 10–50% slower file-heavy work.
- Drafts: `deploy/gvisor/` (install.sh, daemon-evals.json, docker-evals.service, firewall.sh). These are unreviewed
  and unrun.

## Why gVisor rather than SkillEvaluator's Docker mode alone
SkillEvaluator already provides per-trial containers, the same image for both arms, and key handoff via stdin with
redaction. We reuse all of that. On the other hand, it uses default runc on a public network, so the agent holds the
real key inside the container, and the skill side can't set network policy. gVisor plus the firewall plus the gateway
close those gaps.

## Flow
1. Submit a repo URL → discover skills → the submitter picks which ones → a job is queued (submission, rate and queue
   limits apply).
2. Static Tier 1 runs for every skill. It is free and fast, and executes no code.
3. Live Tier 3 runs only for approved skills, on shared category tasks, with and without the skill.
4. Results are tied to the commit and content hash. Unchanged skills are not rerun.
5. Leaderboard: the ranking uses the structural score, security findings are shown separately, and live results are
   labelled by task set. Failed, unsupported and inconclusive results get explicit statuses, never zeros.

## Limits
Hard caps per trial, per day and on total spend. When a cap is reached, the queue pauses rather than overspending.

## Scaling (no redesign needed)
- More live throughput: raise the concurrency cap, or point the worker at E2B or a second VM
  (`--env-mode` / `DOCKER_HOST`). The E2B estimate is about $0.12–0.25 per skill.
- More traffic: add web replicas behind Caddy, and move from SQLite to Postgres.
- Footprint: three containers and one daemon, in one compose file plus one systemd unit.

## Build order (pending go-ahead)
1. Deploy zone A (static only).
2. Install gVisor on the host and test it with SkillEvaluator's bundled sample skill.
3. Set up the gateway and the live worker, piloted on 3 skills.

## Open items
- Host specs (`nproc`, `free -g`, `uname -r`) and the name of Caddy's Docker network.
- `deploy/gvisor/README.md` still names a single `br-evals` 172.31.250.0/24 bridge. Align it with the /20 pool.
- Unverified: two dockerd instances sharing the DOCKER-USER chain, `userns-remap` with runsc, Harbor compose under
  gVisor, and DNS for trials (address the gateway by IP or allow the resolver).
- An NVIDIA Build key (currently 403) or another sponsored model key, needed for live checks.
- SDLC (next discussion): remote, branching, review, CI and deploy path. Nothing gets pushed before then.
