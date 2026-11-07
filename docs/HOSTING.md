# Hosting & sandbox requirements

## Phase 1: static only (Tier 1). No skill code is executed.
- One Linux VM: 2 vCPU, 4 GB RAM, 40 GB disk, Docker, and a TLS reverse proxy (e.g. Caddy) that sets X-Forwarded-For.
- Egress: github.com, objects.githubusercontent.com, pypi.org, files.pythonhosted.org.
- No provider credentials on the box.
- Existing guards: 300 MB repo cap, shallow clone with symlinks off, scans run on a snapshot copy, 600 s timeout,
  submission and lookup rate limits, queue cap.

## Phase 2: live Tier 3. Skill scripts and the agent execute untrusted code.
1. Trials never run on the app host. Preferred: a managed microVM sandbox (E2B, Modal or Daytona; Harbor-native in
   SkillEvaluator). Alternative: a separate dedicated VM with Docker plus gVisor, or Kata (needs nested virtualization).
   `local` mode is refused by `pipeline/tier3.py`.
2. Model key exposure: the in-sandbox agent must reach the LLM. Use a dedicated, hard-capped key held by a gateway
   outside the sandbox. Trials get a short-lived per-run token via the `openai-compatible` provider.
3. Trial egress allowlist: the model gateway only, plus package indexes if a task needs installs.
4. Per-trial CPU, memory, disk, wall-clock and token limits; no host mounts and no Docker socket; ephemeral sandboxes.
   Global daily eval count and spend ceiling: the queue pauses rather than overspending.

## Sandbox cost estimate (2026-11-07; rates from E2B billing docs via secondary sources, not yet measured)
- E2B Hobby: one-time $100 credit, 20 concurrent sandboxes, 1 h sessions. $0.000014/vCPU-s + $0.0000045/GiB-s,
  so 2 vCPU / 4 GiB is about $0.17/h. Daytona has the same rates with a $200 one-time credit. Modal: $30/month recurring.
- Per skill: 4 shared cases × 2 arms × 1 attempt + 1 preflight ≈ 9 sandboxes × 5–10 min ≈ $0.12–0.25.
  So $100 covers about 400–800 skill evaluations. Model calls are separate (NVIDIA Build free tier is rate-limited).
- Next step: a 3-skill pilot on shared-tasks/documents-pdf to replace these estimates with measurements.

## Shared VPS has no /dev/kvm (confirmed 2026-11-07)
That rules out Kata and self-hosted E2B/Daytona/Firecracker. What's left:
- A: a second, dedicated Docker daemon with gVisor (runsc) as its default runtime, used only for evals, with its own
  network, egress firewall and resource caps. Free. Harbor + gVisor compatibility is not yet verified.
- B: managed E2B/Daytona sandboxes. No untrusted code on the VPS. About $0.12–0.25 per skill (estimate).
Recommendation: B for the pilot and launch catalog; revisit A if volume grows.
Needs: E2B key, NVIDIA Build key, egress allowlist for integrate.api.nvidia.com and the E2B API/sandbox domains.
