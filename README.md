# evalskills

A small public leaderboard of AI-agent skills. Submit a public GitHub repository, pick its skills, and see how
[NVIDIA SkillEvaluator](https://github.com/NVIDIA/SkillEvaluator) rates them, with each score labelled by what it
actually measured.

The pipeline: repository URL → skill discovery → evaluation queue → SkillEvaluator → stored results → leaderboard.

## What is in the catalog today

- **166 skills from 35 public repositories, 180 runs** (`catalog/results.jsonl`), all static Tier 1 (SkillEvaluator
  0.8.0). The extra runs are repeat runs for the stability experiment.
- Each run records the repository, skill path, exact commit, content digest, evaluator version and commit,
  duration and status. Agent, model, test cases and baseline are recorded too, and are empty for static runs, which
  use none. Cost is $0 because static runs make no model calls.
- Sanitized reports (`report.public.json`, matched lines stripped) are in `catalog/public-reports.tar.gz`. Raw
  reports stay local and are never committed.
- What the scores mean, and do not mean: `docs/METHODOLOGY.md`. The experiments behind those choices:
  `docs/EXPERIMENT_LOG.md`.

## Layout

* `pipeline/discover.py` finds `SKILL.md` folders in a public GitHub repository.
* `pipeline/evaluate.py` evaluates one skill and appends a provenance record to `catalog/results.jsonl`. The full
  reports go to `reports/` (gitignored).
* `pipeline/server.py` is a stdlib HTTP server with a SQLite store (`data/`), a single evaluation worker, the API and
  the SPA.
* `pipeline/tier3.py` prepares live Tier 3 runs on SkillEvaluator's own cases for each skill (dry-run only until a
  sandbox and key exist). `shared-tasks/` only takes sets vendored from public benchmarks (ADR-0006). Today it
  holds the QuixBugs pilot (`shared-tasks/quixbugs-python/`).
* `web/` holds the single-page app (no build step).
* `deploy/` holds the static-only stack: the app (`Dockerfile`), the scanner sidecar (`scanner/`, #65) and
  `compose.yaml`. It also holds the gVisor eval-daemon drafts (`deploy/gvisor/`, unreviewed and untested).
  `scripts/deploy-smoke.sh` builds both images and runs one real scan through them. CI runs it on PRs that touch
  them.

## Setup

Requirements: Linux, git, Python ≥ 3.11 for the server, and [uv](https://docs.astral.sh/uv/) for SkillEvaluator.
It needs network access to github.com and pypi.org.

```bash
git clone https://github.com/NVIDIA/SkillEvaluator.git ~/vendor/SkillEvaluator   # tested at d136a597c547 (0.8.0)
export SKILLEVALUATOR_DIR=~/vendor/SkillEvaluator
(cd $SKILLEVALUATOR_DIR && uv run --python 3.13 --extra security skillevaluator --version)  # provisions env + scanners

# Evaluate a catalog batch (static Tier 1, about 5–120 s per skill, no API key needed)
while IFS=$'\t' read repo path cat; do python3 -I pipeline/evaluate.py "$repo" "$path" "$cat"; done < catalog/pilot.tsv

# Serve
python3 -I pipeline/server.py --port 8080
```

Useful environment variables are `MAX_QUEUE`, `MAX_SKILLS_PER_SUBMISSION`, `SUBMISSIONS_PER_HOUR`,
`DISCOVERIES_PER_HOUR` and `TIER1_TIMEOUT`. `MAX_SKILLS_PER_SUBMISSION` (default 10) counts only skills that need a
run: a listed skill whose content and evaluator are unchanged is skipped and doesn't count. Put the server behind a TLS reverse proxy that sets
`X-Forwarded-For` for per-client rate limits. The server believes that header only from `TRUSTED_PROXIES` (default:
loopback only), and takes the entry `TRUSTED_PROXY_HOPS` (default 1) places from the right. `deploy/compose.yaml` sets
it for you: Caddy runs on the host and proxies to `127.0.0.1:8088`, so its requests arrive from the gateway of the
app's own bridge network, and that gateway is the one trusted address. Without a trusted proxy, the app sees every
visitor as the proxy and they all share one rate-limit bucket.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/skills` | Leaderboard rows (latest evaluation + job state) |
| GET | `/api/skills/{repo}:{path}` | Detail: score breakdown, context, history |
| POST | `/api/discover` `{url}` | List skills in a repo |
| POST | `/api/submit` `{repo, paths[], category}` | Queue evaluations |
| GET | `/api/queue` | Active jobs |

## Current scope

Only static Tier 1 runs live. It reads the skill and runs no skill code. Live Tier 3 (the agent runs each task with
and without the skill) needs three things: the isolated eval sandbox (a dedicated Docker daemon with gVisor, egress
limited to a model gateway, ADR-0003), a model-provider key, and a model budget or an LLM sponsor. NVIDIA Build's
free trial key may only be used for internal testing and evaluation, so it backs only an internal pilot (#121)
that we run, never a service others can run evaluations on. The pilot's per-skill results may be published
(ADR-0007, amended 2026-11-12). What a funded tier would cost: `docs/SPONSORSHIP.md`. Until then, live
performance shows as "not run" and is never estimated.

## Deploy (static only)

`deploy/compose.yaml` runs two hardened containers. The app is published on `127.0.0.1:8088` only, behind the host's
Caddy. The scanner is SkillEvaluator's Tier 1 image, pinned by digest, with no network, sharing only two exchange
volumes with the app. Copy `deploy/env.example` to `deploy/.env` (no edits needed), run
`docker compose -f deploy/compose.yaml up -d --build`, then add `skills.example.com { reverse_proxy 127.0.0.1:8088 }`
to the Caddyfile and reload Caddy. Untrusted skill code never executes on the app host. The live
sandbox runs separately, as described in `docs/ARCHITECTURE.md`. Hosting notes: `docs/HOSTING.md`.

## Contributing

Read `AGENTS.md`, then `docs/CONVENTIONS.md`. Every change lands by PR, from a typed issue, with green CI. Before
pushing:

```bash
ln -s ../../scripts/pre-commit .git/hooks/pre-commit   # once: secret scan on every commit (needs gitleaks 8.30.1)
uvx ruff check . && uvx ruff format --check .
python3 -m pytest -q
scripts/secret-scan.sh
```
