# Architecture decision records

A record is short: context, decision, consequences. Status moves `proposed → accepted → superseded by ADR-nnnn`. An
accepted record is never edited silently. Add a dated `**Amendment (YYYY-MM-DD, #n):**` block that only adds lines,
or write a new record that supersedes it.

| ADR | Decision |
| --- | --- |
| [0001](0001-rank-on-structure-keep-signals-apart.md) | Rank on the structural score; keep security and live results separate; no fabricated scores |
| [0002](0002-three-trust-zones.md) | Three trust zones: public app, trusted orchestrator, untrusted trials |
| [0003](0003-gvisor-eval-daemon.md) | Live trials run under gVisor on a dedicated Docker daemon, with egress limited to a model gateway |
| [0004](0004-secrets-live-on-the-host.md) | Secrets live only on the host, one container each, as files |
| [0005](0005-development-process.md) | One shared GitHub identity, PR-only, Enes merges |
| [0006](0006-no-self-authored-benchmarks.md) | Live runs use SkillEvaluator's content; shared task sets only from public benchmarks |
