# ADR-0007: No public live tier without a model budget or sponsor

Date: 2026-11-12 · Status: accepted (Enes, 2026-11-12, by direct message; amended the same day, below)

## Context
#121 planned live checks as an optional, operator-approved public layer on NVIDIA Build's free API key, with one
fixed agent and model (OpenCode + Nemotron). The key is governed by the NVIDIA API Trial Terms of Service
(v. September 19, 2025):
- §1.2: access is "for limited trial purposes only and without use of the API Service or Generated Content in
  production."
- §1.4: without a paid subscription, "you may only use the API Service for internal testing and evaluation
  purposes, not in production."
- §4.2: you may not "distribute or make available to others any portion of the API Service or Generated Content."
- §11.2: NVIDIA "may at any time terminate the availability, or your use, of the API Service."
- §3.3: NVIDIA may use inputs and outputs "to improve NVIDIA products and services, including AI models."

A public board that shows results built from this key, whether live trials, an LLM-assisted security scan
(SkillSpector's `--llm` stage) or embeddings, is production use, and arguably makes the service available to
others. NVIDIA can also end the key at any time, so it can't carry a public promise. Every other model provider
charges per token. Today every public record is static (`tier1-static`, no LLM).

## Decision
1. No public result of any kind comes from the trial key. There is no public live tier until it has a dedicated
   model budget or an LLM sponsor, and the README and METHODOLOGY say so plainly. Static checks (Tier 1 and
   security, without `--llm`) stay the default for every submission, at $0.
2. The #121 pilot still runs on the free key, as internal evaluation: ten skills, run by us, to measure runtime,
   requests and tokens per skill.
   - Its records stay out of the public bundle and off the board: they go to a private file under `reports/`, never
     to `catalog/results.jsonl`. The public build rejects any `mode: tier3` record, with a test.
   - Agent transcripts, raw reports and per-case judge verdicts are Generated Content (the judge is Nemotron too)
     and stay private.
   - E12 publishes per-skill aggregates only: lift, spread between runs, runtime, requests and tokens.
3. A sponsorship table (`docs/SPONSORSHIP.md`) lists candidate models with dated list prices and the cost formula.
   Its catalog totals stay empty until E12 measures tokens per skill. A total is never estimated ahead of a
   measurement.
4. Sponsored runs keep the board's rules:
   - one fixed agent and model per live column, so scores stay comparable within it; models are never mixed into
     one score;
   - the sponsor is named next to every result it paid for;
   - the sponsor has no say over which skills run or how they are scored;
   - the caps and the sandbox (ADR-0003, ADR-0004) still apply.

## Consequences
- The board keeps saying live performance is not measured. METHODOLOGY §3 stays "not yet available" and states
  why.
- The gateway reads its upstream base URL and model allowlist from config, so a paid provider or a sponsor's
  endpoint is a configuration change, not a redesign.
- Nemotron's weights are open, so a paid Nemotron host lets the pilot's runs calibrate a funded tier on the same
  model. Token counts from the pilot don't transfer to other models (different tokenizers, step counts and
  caching), so a sponsor's estimate needs a few runs on their model first.
- A public result that needs a model, such as Tier 2 on a local embedding model, needs its own issue, and the
  model's licence is checked there.
- Revisit when funding is offered, or if NVIDIA's terms change.

**Amendment (2026-11-12, #130):** decision 1 is narrowed. Decided by Enes, 2026-11-12, by direct message.
- What the trial key never backs is a service: nobody but us runs an evaluation on it, whether by submitting a
  skill, choosing what runs, or triggering a run. That is what §4.2 ("make available to others") rules out here.
- The pilot's results may be published: per-skill scores, lift, spread between runs, runtime, requests and tokens.
  Agent transcripts, raw reports and per-case judge verdicts still stay private (decision 2).
- Where they appear, E12 only or also on the board as labelled pilot results, is decided on #130. Until then,
  decision 2's guard stands: the public build rejects any `mode: tier3` record.
- The README and METHODOLOGY §3 say this, in place of "no public result of any kind".

**Amendment (2026-11-12, #132):** the open question above is answered. Decided by Enes, 2026-11-12, by direct
message. The pilot's published results appear in `docs/EXPERIMENT_LOG.md` (E12) only. The board shows none of them,
and decision 2's guard stays: the public build rejects any `mode: tier3` record. Revisit later.
