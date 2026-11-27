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
* Bias: the rubric encodes NVIDIA's SKILL.md conventions (`metadata.tags`, "Use when…" triggers, script tables).
  Skills written to that format score higher (median 93.8 for NVIDIA's 19 skills, 81–91.5 for other publishers
  with three or more). Since v1.0.1 (E13), a missing root `version` is no longer deducted. It is also
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

* Sources: the SkillEvaluator Tier 1 scanners: SkillSpector 2.13.4 (rules only, no LLM), Gitleaks, Bandit/Semgrep,
  PII, dependency audit and Unicode smuggling.
* Counts are **distinct finding types** per severity. For example, 85 unpinned `npx` calls count as one "MCP Rug Pull"
  finding. The raw counts are kept in the record.
* These are static pattern matches. They point to places a human should look, not to confirmed vulnerabilities.
* `partial`: SkillSpector often reports incomplete analysis because SKILL.md code samples mention example files
  such as `document.pdf` that it cannot resolve. In that case the result view says `partial scan` and explains that
  findings may be missing. Leaderboard rows leave it out, because it applies to about half the skills (80 of 166,
  E13), but a partial scan never shows `clean`. It does not hide the issue or treat it as a pass.
* Schema governance findings (for example, missing `metadata.author`, which v1.0.1 rates low) are kept out of the
  security counts.

## 3. Live performance (Tier 3), not yet available

Tier 3 runs an agent on test cases with the skill and without it (the baseline) and reports Skill Lift. This
deployment can't run it publicly yet. The isolated sandbox is not installed, and a public live tier needs a model
budget or an LLM sponsor (ADR-0007). The leaderboard shows "not run" for every skill. It never shows an estimate.

Why a free key is not enough: NVIDIA Build's free key comes under the NVIDIA API Trial Terms of Service, which allow
internal testing and evaluation only, not production, and not making the service or its output available to
others. Our reading (Enes, ADR-0007 as amended 2026-11-12): nobody but us runs anything on that key, and we may
publish our own pilot's measured results. So no submission triggers a live run, an LLM-assisted security scan or
embeddings on it. It backs one internal pilot (#121), which we run on ten skills we chose. Its per-skill results
(scores, lift, spread between runs, runtime, requests, tokens) may be published, labelled as the pilot's. Agent
transcripts, raw reports and per-case judge verdicts stay private, and on the host only root can read them (#256). A
smoke run (one case, one run, marked `smoke`) only checks that the pipeline works before a full run, and is never
reported as a result. A pilot run whose skill the agent never loaded is `inconclusive`, with no score, as
SkillEvaluator itself judges it: both arms ran and were graded, but there is no lift to measure (#265, E17).

From 2026-11-20 (#236), the pilot's trials have open web access. The agent and the skill could reach any public
site, including published solutions to the pilot's cases: QuixBugs' fixed programs are public. Until a pilot result
can show the agent didn't look an answer up, read its lift as an upper bound on what the skill contributes, and not
as a clean measure. Every pilot record says which it ran under: `egress` is `open` or `allowlist`, read from the
running proxy before each run. A tier beyond the pilot goes back to a short allowlist (ADR-0003, amended
2026-11-20).

A funded tier would keep one fixed agent and model per live column, so its scores are comparable only within that
column. Models are never mixed into one score. A provider can retire a model, as NVIDIA did with the pilot's
first one (E14). A result keeps the model it ran on, and a column whose model is retired starts over on its successor;
old and new results aren't compared or merged. The sponsor is named next to every result it paid for, and has no
say over which skills run or how they are scored. Costs and terms: `docs/SPONSORSHIP.md`.

Live runs use SkillEvaluator's own content for each skill (ADR-0006): the test cases the skill publishes in its
`evals/`, or the starter case SkillEvaluator prepares when it has none. Every skill is therefore tested on
different cases, so a live result says how much this skill helped on its own cases, and is not comparable across
skills, even within a category. When live results are added, they will show which cases were used.

A skill doesn't grade itself. Before a run, any grader, grading config, native Harbor task or custom trial
environment it ships is removed, and SkillEvaluator's default judge scores its cases. Because the author still wrote
the cases, such a result is labelled **self-reported**. A run on SkillEvaluator's starter case, because the skill
ships no cases, is labelled **starter**. SkillEvaluator writes that one case itself (its autopilot): with a judge model
configured (`SKILL_EVAL_LLM_MODEL`; in the pilot, the agent's model), that model writes it from the skill's SKILL.md.
Its only input is the skill's own text, so the skill's author can steer it, as with self-reported cases. A
starter result therefore reads as one model-written case, not a benchmark (#268).

We don't write our own benchmark tasks. Cross-skill comparison would need a shared task set vendored from a
public benchmark, with its source and licence recorded. The first is a pilot for testing-debugging: six
programs from QuixBugs (MIT CSAIL, 2017), graded with QuixBugs' own test cases (`shared-tasks/quixbugs-python/`).
It hasn't run live yet.

## Statuses

| Status | Meaning |
| --- | --- |
| queued | Waiting for the single evaluation worker, or for the scanner to pick the job up. The job says which |
| running | SkillEvaluator is running. A deployed scan is running once the scanner has picked the job up |
| completed (Evaluated) | A report was produced. Scores come from it |
| failed | The evaluator ran but produced no report (timeout, crash). No score is shown |
| unsupported | The path has no SKILL.md at the current commit, or the repo can't be read |

SkillEvaluator's own `overall_status` is almost always "failed" for third-party skills under the default policy. The
reasons are the author-metadata rule and SkillSpector partial analysis. It is shown in the detail view, not used as a
verdict.

## Versioning and re-runs

Every result records the repository, skill path, exact commit, a SHA-256 digest of the skill folder, the evaluator,
the mode, the duration and the cost. The evaluator is recorded in one of two ways:

* **Deployed scans** (the scanner sidecar, #65) record the SkillEvaluator version and the digest of the
  `skillevaluator-tier1` image they ran in, with no commit. The digest pins SkillEvaluator's own base image, which
  holds the evaluator and its scanners. Our scanner image adds only a small driver (`deploy/scanner/scanner.py`) on
  top, which does not take part in scoring. The digest comes from the app's own configuration, not from the scanner.
* **Local runs** (a development checkout, and the initial catalog) record the SkillEvaluator version and source
  commit, with scanners provisioned by uv.

A resubmission reuses the existing completed result, instead of running again, when the folder digest and the
evaluator are unchanged: the image digest when there is one, else the commit. Moving to a new image digest re-runs
skills on their next submission. A failed run is never reused: resubmitting the same skill runs it again.

The initial catalog comes from SkillEvaluator `d136a59`. Whether the pinned image scores the same content the same
way is checked in the experiment log (E10). Since 2026-11-20 the pin is the v1.0.1 image
(`skillevaluator-tier1@sha256:fbff256d…`), and the whole catalog was re-scanned with it (E13). 1.0 changed the
rubric, so most scores rose by 1.7 points. Don't compare a score across that change. Each result names its engine.

## Safety

* Tier 1 is static. Skill code is scanned, never executed.
* Repositories are cloned shallowly with symlinks disabled. Only the skill folder is copied into a temporary
  snapshot for scanning.
* The evaluator subprocess gets an allow-listed environment. Service credentials are never passed to the
  static run.
* Limits: repositories up to 300 MB. The size is checked with the GitHub API before cloning, and again on disk after
  cloning, where the shallow checkout including `.git` counts, so the effective limit is slightly stricter than the
  API's figure. When the API can't give the size, the lookup is refused rather than cloned. Also: 10 skills per
  submission, 20 submissions and 30 lookups per client per hour, 100 queued jobs, and a 900 s timeout per evaluation.
  Inside it, SkillSpector's own deadlines (630 s, then a 720 s watchdog) end a slow security scan first. The report
  is kept, with Security Scan `incomplete`, but a scan cut short can show fewer distinct risks than a full one. About
  1% of the catalog needs that long (#112).
* Cloned repositories are deleted after 6 hours without use, and at most 40 are kept (least recently used go first).
  A clone that a queued or running job needs is never deleted. After a deletion, a submission needs a fresh lookup.
