# ADR-0006: Live runs use SkillEvaluator's content; shared task sets only from public benchmarks

Date: 2026-11-09 · Status: accepted (Enes, 2026-11-09, by direct message)

## Context
SkillEvaluator's own cases for a skill come from the skill itself: its `evals/` when it ships them, otherwise a
starter case SkillEvaluator prepares. Each skill gets different cases, so live results are not comparable across
skills (E5). We had written our own shared task sets with graders (`shared-tasks/documents-pdf`,
`testing-debugging`, and a proposed code-review set in #58) to make them comparable. None of them has run live.

A benchmark we author is content we have to defend: that its tasks are fair, representative and not tuned to any
skill. A small project can't make that case convincingly yet.

## Decision
1. Live (Tier 3) runs use SkillEvaluator's own content: the skill's published `evals/`, or the starter case
   SkillEvaluator prepares when there are none. Results are per-skill Skill Lift, labelled as such.
2. We don't author benchmark tasks or graders. A shared task set is added only when it is vendored and adapted
   from a public benchmark. The set carries a `SOURCE.md` naming the upstream benchmark, its exact revision and its
   licence, and saying what was adapted. `pipeline/tier3.py` refuses a set without one.
3. On its own cases, a skill doesn't grade itself (Enes, 2026-11-09). Before the run, `pipeline/tier3.py` removes
   its graders (`evals/grader.{py,sh}`, `evals/tests/`), native Harbor tasks (`evals/harbor/`), grading config
   (`evals/config.{yml,yaml}`) and custom trial environment (`evals/environment/`). Its cases and input files stay,
   and SkillEvaluator's default judge scores them. The result is labelled **self-reported**, because the author
   wrote the cases.
4. The self-authored sets are removed. They stay in git history and in E5 and E6 as experiments.

## Consequences
- Until a public benchmark is vendored for a category, live results are not comparable across skills. The UI and
  METHODOLOGY say so, and ADR-0001's rule (compare only on the same task set) still holds.
- A skill's own `evals/` and grader are untrusted input. They run inside the trial container, like the skill
  itself, so under ADR-0003 they run under gVisor. SkillEvaluator d136a59 copies `evals/grader.py` into the task's
  `tests/` (`tier3/harbor/adapter.py`, `_copy_custom_grader`) and calls it from `tests/test.sh` (`_write_test_sh`).
  Harbor 0.24.0 uploads `tests/` into the trial environment and runs `test.sh` there
  (`harbor/verifier/verifier.py`, `Verifier.verify`: `environment.upload_dir`, then `environment.exec`, which
  for the docker environment is `docker compose exec` in the trial's own container). The verifier step also
  receives the judge's provider variables (`_verifier_env_vars`), so a skill's grader can read the per-run gateway
  token, bounded by the gateway's budget (ADR-0004).
- Finding public benchmarks per category, with licences compatible with redistribution, is research to do before
  any set is added. Enes approves each one.
