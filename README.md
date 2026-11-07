# Skill Inventory

A public leaderboard of agent skills, evaluated with [NVIDIA SkillEvaluator](https://github.com/NVIDIA/SkillEvaluator).

The pipeline: repository URL → skill discovery → evaluation queue → SkillEvaluator → stored results → leaderboard.

* `pipeline/evaluate.py` evaluates one skill and appends a provenance record to `catalog/results.jsonl`. The full
  reports go to `reports/`.
* `pipeline/discover.py` finds `SKILL.md` folders in a public GitHub repository.
* `pipeline/server.py` is a stdlib HTTP server with a SQLite store (`data/`), a single evaluation worker, the API and
  the SPA.
* `web/` holds the single-page app (no build step).
* `docs/METHODOLOGY.md` explains what scores mean. `docs/EXPERIMENT_LOG.md` covers the experiments behind that.

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
`X-Forwarded-For` for per-client rate limits.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/skills` | Leaderboard rows (latest evaluation + job state) |
| GET | `/api/skills/{repo}:{path}` | Detail: score breakdown, context, history, install |
| POST | `/api/discover` `{url}` | List skills in a repo |
| POST | `/api/submit` `{repo, paths[], category}` | Queue evaluations |
| GET | `/api/queue` | Active jobs |

## Current scope

Only static Tier 1 runs live. Tier 3 (agent runs with and without the skill) needs a model-provider key and an
isolated sandbox (Docker or Modal/E2B/Daytona). Neither is configured, so live performance shows "not run" and is
never estimated.

## Deploy (draft, not yet built: no Docker on the dev workstation)

`deploy/Dockerfile` pins SkillEvaluator. `deploy/compose.yaml` runs the app on a Docker VM. Phase 1 only (static). Live Tier 3 must use a managed sandbox, never this host; see docs/HOSTING.md. The earlier `live`
profile adds a separate Docker-in-Docker daemon for Tier 3 sandboxes, so the host socket is never mounted.
Copy `deploy/env.example` to `deploy/.env`, then run `docker compose -f deploy/compose.yaml up -d --build`.
