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
* `deploy/` holds the static-only container (`Dockerfile`, `compose.yaml`) and the gVisor eval-daemon drafts
  (`deploy/gvisor/`, unreviewed and untested).

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
`DISCOVERIES_PER_HOUR` and `TIER1_TIMEOUT`. Put the server behind a TLS reverse proxy that sets
`X-Forwarded-For` for per-client rate limits. The server believes that header only from `TRUSTED_PROXIES` (default:
loopback only), and takes the entry `TRUSTED_PROXY_HOPS` (default 1) places from the right. **In a deployment, pin
Caddy to a fixed address on the `web` network and set `TRUSTED_PROXIES` to it in `deploy/.env`.** If it is not set,
the app sees every visitor as Caddy and they all share one rate-limit bucket.

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
and without the skill) is blocked on two things: a model-provider key, and the isolated eval sandbox (a dedicated
Docker daemon with gVisor, egress limited to a model gateway). See ADR-0003 in `docs/adr/`. Until then, live
performance shows as "not run" and is never estimated.

## Deploy (static only, not yet deployed)

`deploy/Dockerfile` pins SkillEvaluator, and `deploy/compose.yaml` runs the app as one hardened container behind a
TLS reverse proxy. Copy `deploy/env.example` to `deploy/.env`, then run
`docker compose -f deploy/compose.yaml up -d --build`. Untrusted skill code never executes on the app host. The live
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
