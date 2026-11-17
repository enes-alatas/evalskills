# ADR-0008: Secrets are env vars the host sets, one container each

Date: 2026-11-17 · Status: accepted (Enes, 2026-11-17, by direct message; recorded on #167) · Supersedes the
delivery rule of [ADR-0004](0004-secrets-live-on-the-host.md)

## Context
ADR-0004 delivered secrets as files mounted at `/run/secrets/…`, because `docker inspect` shows a container's
environment and child processes inherit it. #167 added zone A's first secret, a GitHub token for repository
lookups, and Enes decided that it, and every token from now on, is an environment variable the host sets.

## Decision
- A secret is an environment variable, set on the host for the one container that needs it: in the environment of the
  compose command, or in that host's `deploy/.env` (mode `0600`, ignored by git). It is never in the repository, CI,
  an image, `env.example`, a log, an answer or a fixture. ADR-0004's other rules stand.
- **"The host must set it":** compose names it with `${NAME?…}`, so a deploy refuses to start while it is unset.
  Where a secret is optional, an empty value is the explicit "none": development and CI set it empty, and CI never
  holds a token. For `GITHUB_LOOKUP_TOKEN`, empty means unauthenticated lookups, and the smoke test sets it so.
- **Child processes never inherit one.** The process that reads it takes it out of its own environment at start
  (`os.environ.pop`), and every subprocess gets the allowlisted environment only (`evaluate.git_env()`, `ENV_ALLOW`;
  `sh()` uses it by default). No secret is ever on that list. A test asserts both.
- **Exposure, the trade-off ADR-0004 named:** `docker inspect` on the host, and `/proc/1/environ` inside the
  container, still show the value as it was at start. Both need the host's Docker or the container's own user, and
  who holds those already holds the host. So a secret delivered this way must be one whose leak is bounded: the lookup
  token grants public read access only, and the site spends at most `LOOKUPS_PER_HOUR` of its owner's quota.
- The model gateway's key is still a mounted file. It moves to this rule in its own change, #218; until then
  ADR-0004's delivery rule applies to it alone.

## Consequences
- Setting or rotating a secret means editing the host's env and recreating that container; there is no live reload.
- Code that spawns a process passes an explicit environment. A bare `subprocess.run` in zone A is a review finding.
