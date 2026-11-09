# ADR-0001: Rank on the structural score, keep the other signals apart, never fabricate a score

Date: 2026-11-08 · Status: proposed (needs Enes's acceptance on #6). Evidence: experiments E1–E6 in `docs/EXPERIMENT_LOG.md`

**Amendment (2026-11-08, #31):** Accepted as written by Enes on 2026-11-08, so the status is now accepted. The
noise band in decision 1 is delivered in #47.

## Context
SkillEvaluator gives three different kinds of evidence. Tier 1 gives a deterministic structural quality score, and
static security scanner findings. Tier 3 gives a live with-skill vs baseline result. The experiments on 123 skills
from 16 repositories showed:
- SkillEvaluator's own `overall_status` is "failed" for every third-party skill under the default policy, mostly
  because of the `metadata.author` rule and SkillSpector's "partial" analysis. It cannot rank anything.
- The structural score is repeatable, but narrow (71.5–98.8), biased towards NVIDIA's SKILL.md format, and moves
  about ±2–3 points between copies of the same skill.
- Scanner findings are pattern matches, and a crashed SkillSpector run looked clean until the pipeline detected it.
- Keyless auto-generated Tier 3 cases echo the skill description, so they are not comparable across skills.

## Decision
1. The leaderboard is ordered by the Tier 1 structural score, with the noise band stated beside it.
2. Security findings are shown as counts of distinct finding types per severity, never folded into the rank.
   `partial` and `scanner error` are shown as such.
3. Live (Tier 3) results are shown separately, labelled with the task set they ran on. They are compared only
   between skills that ran the same shared category tasks. A shared category alone is not enough.
4. A run that produced no usable result is `failed`, `unsupported` or `inconclusive`, never a 0 and never ranked.
5. Every result is tied to the repository, path, commit, folder digest and evaluator version. An unchanged digest
   and evaluator are not rerun.

How each score is computed and read is in `docs/METHODOLOGY.md`, which changes in the same PR as any change here.

## Consequences
- The rank says "well-formed by SkillEvaluator's rubric", not "works better". The UI and METHODOLOGY must keep
  saying so.
- Comparable live numbers need shared tasks with deterministic graders (`shared-tasks/`), which costs authoring time
  per category.

**Amendment (2026-11-09, #61):** shared category tasks now come only from vendored public benchmarks, never from
sets we author (ADR-0006). The self-authored `shared-tasks/` sets are removed. Until a public set exists for a
category, live results are per-skill Skill Lift on each skill's own cases and are not compared across skills.
