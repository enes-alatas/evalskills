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
* Noise band: about ±2–3 points. Two copies of the same skill with small format drift scored 74.5 and 76.8 (E6, one
  pair). Scores within 3 points of each other should be read as ties. The result view says so. The leaderboard
  does not, because a bare tie line confused readers without this context. Real content changes can move the score
  by much more (E4: 74.0 vs 84.8 for two different pdf skills), and that is a real difference, not noise.
* The leaderboard is ordered by this score, highest first. Ties are ordered alphabetically. A skill with no completed
  evaluation gets no rank and no score.

## 2. Security findings (shown, not ranked)

* Sources: the SkillEvaluator Tier 1 scanners: SkillSpector 2.13.3 (rules only, no LLM), Gitleaks, Bandit/Semgrep,
  PII, dependency audit and Unicode smuggling.
* Counts are **distinct finding types** per severity. For example, 85 unpinned `npx` calls count as one "MCP Rug Pull"
  finding. The raw counts are kept in the record.
* These are static pattern matches. They point to places a human should look, not to confirmed vulnerabilities.
* `partial`: SkillSpector often reports incomplete analysis because SKILL.md code samples mention example files
  such as `document.pdf` that it cannot resolve. In that case the result view says `partial scan` and explains that
  findings may be missing. Leaderboard rows leave it out, because it applies to most skills, but a partial scan
  never shows `clean`. It does not hide the issue or treat it as a pass.
* Schema governance findings (for example, missing `metadata.author`, which the default NVIDIA publication policy rates
  high) are kept out of the security counts.

## 3. Live performance (Tier 3), not yet available

Tier 3 runs an agent on test cases with the skill and without it (the baseline) and reports Skill Lift. This
deployment can't run it yet because it has no model-provider credential and no isolated sandbox. The leaderboard
shows "not run" for every skill. It never shows an estimate.

Live runs use SkillEvaluator's own content for each skill (ADR-0006): the test cases the skill publishes in its
`evals/`, or the starter case SkillEvaluator prepares when it has none. Every skill is therefore tested on
different cases, so a live result says how much this skill helped on its own cases, and is not comparable across
skills, even within a category. When live results are added, they will show which cases were used.

A skill doesn't grade itself. Before a run, any grader, grading config, native Harbor task or custom trial
environment it ships is removed, and SkillEvaluator's default judge scores its cases. Because the author still wrote
the cases, such a result is labelled **self-reported**.

We don't write our own benchmark tasks. Cross-skill comparison would need a shared task set vendored from a
public benchmark, with its source and licence recorded. The first is a pilot for testing-debugging: six
programs from QuixBugs (MIT CSAIL, 2017), graded with QuixBugs' own test cases (`shared-tasks/quixbugs-python/`).
It hasn't run live yet.

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
* Limits: repositories up to 300 MB. The size is checked with the GitHub API before cloning, and again on disk after
  cloning, where the shallow checkout including `.git` counts, so the effective limit is slightly stricter than the
  API's figure. When the API can't give the size, the lookup is refused rather than cloned. Also: 10 skills per
  submission, 20 submissions and 30 lookups per client per hour, 100 queued jobs, and a 600 s timeout per evaluation.
* Cloned repositories are deleted after 6 hours without use, and at most 40 are kept (least recently used go first).
  A clone that a queued or running job needs is never deleted. After a deletion, a submission needs a fresh lookup.
