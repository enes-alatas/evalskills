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

**Amendment (2026-11-11, #65):** Static Tier 1 leaves the app container, which closes the 11-09 exception above.
Enes approved this design on #65 on 2026-11-11.
- **The scanner:** a `scanner` sidecar in the same compose file, built `FROM` the digest-pinned SkillEvaluator Tier 1
  image that CI uses. It runs with `network_mode: none`, read-only, all capabilities dropped, non-root, with no env
  file and no Docker socket. It mounts only two exchange volumes: the job inbox read-only and the report outbox.
- **One scan per container life:** it scans one job and exits, and `restart: always` starts a fresh container, so
  nothing persists from one scan to the next.
- **The app:** zone A writes the skill snapshot to the inbox and reads back only the report files, as regular files.
  It no longer installs SkillEvaluator or keeps a uv cache. Each run records the image digest as its evaluator.
- **No gVisor for static scans:** the scanner runs on the main daemon under runc, not on `docker-evals`. Tier 1
  parses and scans skills but executes none of their code, and the sidecar needs no socket in any container. Live
  Tier 3 still runs only under gVisor (ADR-0003).
- **When the scanner is down:** a job no scanner picks up goes back to the queue and stays `queued`. A scan that
  overruns its time cap is `failed` ("scan timed out").
