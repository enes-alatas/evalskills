# ADR-0004: Secrets live only on the host, one container each, as files

Date: 2026-11-08 · Status: accepted (Enes, 2026-11-07, by direct message)

## Context
The repository is private now and will be public later. Anything committed, put in CI, or baked into an image must
be safe to publish.

## Decision
- No secret in the repository, CI, images, logs, API responses, fixtures or chat. CI uses no secrets at all.
- Secrets are set on the host only, in a root-owned `0600` file, and delivered to exactly one container. The model
  key goes to the gateway and nowhere else.
- Deliver them as mounted files (`/run/secrets/…`), not plain environment variables. `docker inspect` shows a
  container's environment, and child processes inherit it.
- The worker → SkillEvaluator → agent chain never inherits the real key. Trials get a per-run gateway token.
- gitleaks runs in CI over every PR's added commits and in a pre-commit hook (`scripts/secret-scan.sh`).

## Consequences
- Enes owns deployment and the secret files. Automating deploys later must not hand a secret to code from a public PR.
- Before the repo goes public, run a full-history scan, and add a workflow that redacts credentials out of issue and
  comment bodies.
