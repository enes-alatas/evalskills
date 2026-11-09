# ADR-0002: Three trust zones

Date: 2026-11-08 · Status: accepted (Enes, 2026-11-07, by direct message)

## Context
Submitted repositories are untrusted: their scripts run during live evaluation, and their text reaches parsers and
the UI. The service also needs a model key for live runs. Everything runs on one shared VPS that hosts Enes's other
apps behind Caddy, and it has no `/dev/kvm`.

## Decision
| Zone | Runs | Holds | May reach |
| --- | --- | --- | --- |
| A: public app | Web + API, static Tier 1 worker, SQLite, sanitized public reports | No secrets | GitHub (read-only clones), its own volume |
| B: orchestration | Live worker, LLM gateway | The model key (gateway only) | The eval daemon's socket only, the model API |
| C: untrusted execution | Ephemeral trial sandboxes (agent, skill scripts, grader) | A per-run gateway token only | The gateway only |

- Zone A parses untrusted input and never executes it. Its container has a read-only filesystem, drops all
  capabilities and runs as non-root.
- No container in any zone gets the main Docker socket.
- The app never runs inside an agent's workstation, because that workstation holds the agent's own credentials.
- Static scans should run the way spool's ADR-0065 runs them: a digest-pinned SkillEvaluator image,
  `--network none`, a read-only mount and no secrets.

## Consequences
- A bug in zone A exposes no secret and can't start code execution. A compromised trial reaches only the gateway,
  with a token capped in time and budget.
- Three containers and one extra daemon, in one compose file plus one systemd unit.

**Amendment (2026-11-09, #32):** static Tier 1 runs as a `skillevaluator tier1` subprocess inside the zone A app
container (`pipeline/evaluate.py`), not in the pinned image this ADR names. Enes accepted this on 2026-11-09 as an
interim exception.
- **Why it is acceptable for now:** Tier 1 parses and scans the skill and executes none of its code (`--no-llm`, no
  model key). The container is non-root, has a read-only root filesystem and drops all capabilities. Zone A holds
  no secrets, so a compromised scanner has no credential to take.
- **What it gives up against the pinned `--network none` image:** a scanner or parser bug triggered by a crafted
  skill would run with the app's network access and its uid. It could reach the network and read or rewrite all
  four writable volumes: `data` (SQLite), `reports`, `cache` (clones) and `uvcache` (`~/.cache/uv`). `uvcache` is
  the worst case, because SkillSpector runs from it via `uv tool run`: code planted there would persist and run on
  every later scan, beyond the one that planted it. The evaluator is pinned by the commit and lockfile the app image
  is built from, not by a separate image digest recorded with each run.
- **Planned end state:** #65. Zone B runs the digest-pinned image on the `docker-evals` daemon with
  `--network none`, and zone A reads back only the report. That move closes this exception.
