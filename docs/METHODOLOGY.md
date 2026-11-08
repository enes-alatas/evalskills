# How evalskills scores skills

evalskills runs NVIDIA SkillEvaluator on public agent skills. It keeps three kinds of evidence separate and
never combines them into a single number.

## 1. Structural quality (the ranking column)

* Source: SkillEvaluator Tier 1 deterministic quality rubric (`quality_summary` in `skillevaluator-tier1.json`).
* What it measures: the SKILL.md frontmatter, its sections and the folder layout. The score has four weighted
  dimensions: correctness 0.35, discoverability 0.25, reliability and efficiency. The scale is 0–100, with letter grades.
* What it does **not** measure: whether an agent does a better job with the skill. A clearly written skill that
  gives bad advice can still score well.
* Comparability: every skill gets the same rubric, so these scores can be compared across the catalog and within a
  category. The rubric does treat script-based and guide-only skills a bit differently (see `skill_type` in the
  report).
* Bias: the rubric encodes NVIDIA's SKILL.md conventions (version, `metadata.tags`, "Use when…" triggers, script
  tables). Skills written to that format score higher (NVIDIA median 95.6 vs 79–90 for other publishers). It is also
  coarse: two forks with different SKILL.md content can get the same score.
* Repeatability: it is deterministic. The same content and the same evaluator version give the same score (see the
  experiment log).
* The leaderboard is ordered by this score, highest first. Ties are ordered alphabetically. A skill with no completed
  evaluation gets no rank and no score.

## 2. Security findings (shown, not ranked)

* Sources: the SkillEvaluator Tier 1 scanners: SkillSpector 2.13.3 (rules only, no LLM), Gitleaks, Bandit/Semgrep,
  PII, dependency audit and Unicode smuggling.
* Counts are **distinct finding types** per severity. For example, 85 unpinned `npx` calls count as one "MCP Rug Pull"
  finding. The raw counts are kept in the record.
* These are static pattern matches. They point to places a human should look, not to confirmed vulnerabilities.
* `partial`: SkillSpector often reports incomplete analysis because SKILL.md code samples mention example files
  such as `document.pdf` that it cannot resolve. In that case the leaderboard says `partial`. It does not hide the issue
  or treat it as a pass.
* Schema governance findings (for example, missing `metadata.author`, which the default NVIDIA publication policy rates
  high) are kept out of the security counts.

## 3. Live performance (Tier 3), not yet available

Tier 3 runs an agent on test cases with the skill and without it (the baseline) and reports Skill Lift. This
deployment can't run it yet because it has no model-provider credential and no isolated sandbox. The leaderboard
shows "not run" for every skill. It never shows an estimate.

Tier 3 results won't be comparable across skills that were evaluated on different tasks. When live results are
added, they will show which test cases were used, and comparisons will be limited to skills that ran on the same
shared category tests.

## Statuses

| Status | Meaning |
| --- | --- |
| queued | Waiting for the single evaluation worker |
| running | SkillEvaluator is running |
| completed (Evaluated) | A report was produced. Scores come from it |
| failed | The evaluator ran but produced no report (timeout, crash). No score is shown |
| unsupported | The path has no SKILL.md at the current commit, or the repo can't be read |

SkillEvaluator's own `overall_status` is almost always "failed" for third-party skills under the default policy. The
reasons are the author-metadata rule and SkillSpector partial analysis. It is shown in the detail view, not used as a
verdict.

## Versioning and re-runs

Every result records the repository, skill path, exact commit, a SHA-256 digest of the skill folder, the
SkillEvaluator version and commit, the mode, the duration and the cost. If the folder digest and the evaluator commit
are unchanged, a resubmission reuses the existing completed result instead of running again. A failed run is never
reused: resubmitting the same skill runs it again.

## Safety

* Tier 1 is static. Skill code is scanned, never executed.
* Repositories are cloned shallowly with symlinks disabled. Only the skill folder is copied into a temporary
  snapshot for scanning.
* The evaluator subprocess gets an allow-listed environment. Service credentials are never passed to the
  static run.
* Limits: repositories up to 300 MB, 10 skills per submission, 20 submissions and 30 lookups per client per hour,
  100 queued jobs, and a 600 s timeout per evaluation.
