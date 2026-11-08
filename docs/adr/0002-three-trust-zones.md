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
