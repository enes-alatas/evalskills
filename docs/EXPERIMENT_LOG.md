# Experiment log

## 2026-11-07 — setup
- Workstation: Debian 12, no Docker, no user namespaces (bwrap fails), 0 capabilities. Egress via proxy:
  github/pypi/npm/api.anthropic.com reachable; integrate.api.nvidia.com and astral.sh blocked (403).
- SkillEvaluator 0.8.0 @ d136a597c547 (NVIDIA/SkillEvaluator main, 2026-11-07). Run via `uv run --python 3.13` from checkout.
- No provider key in env -> only keyless Tier 1 (static) possible. Tier 3 also needs an isolated sandbox
  (docker / modal / e2b / daytona); `local` mode would run untrusted skills on host under no sandbox here -> refused.

## E1 — Tier 1 static pilot (5 skills: anthropics pdf/docx/xlsx/pptx, openai pdf)
- Runtime 5–66 s/skill (docs/xlsx/pptx ~60 s: many bundled scripts). Cost $0 (no model calls).
- All 5 exit 1 / overall "failed" under the default policy. Causes, none of which are skill-quality signals:
  1. `SCHEMA.author_missing` is overridden to **high** in the default (NVIDIA publication) profile -> any skill without
     `metadata.author` fails. Most public skills don't set it.
  2. SkillSpector 2.13.3 marks analysis `partial` when SKILL.md code samples mention example filenames
     (`PdfReader("document.pdf")` -> `reference_missing`) -> Security Scan = INCOMPLETE. Environment/tool artifact.
- The deterministic quality score (0–100, 4 weighted dimensions: correctness .35, discoverability .25,
  reliability, efficiency) is produced for every skill and is the usable structural signal.
- Decision (provisional): never rank on Tier 1 overall pass/fail. Show structural quality score; show security
  findings by severity separately; show scanner INCOMPLETE explicitly.

## E2 — Tier 1 static pilot, full (38 skills, 9 categories, 8 repos)
- 38/38 produced reports. Median 7.1 s, max 117 s (cloudflare-deploy: big references tree). 717 s total. $0.
- Every skill fails `Schema & Repository Governance` (author metadata = high under the default policy) -> SkillEvaluator
  `overall_status` = failed for 38/38. It doesn't discriminate between skills; it's kept out of ranking.
- Security Scan: 20 failed, 15 incomplete (SkillSpector `partial`), 4 passed. Raw finding counts are dominated by
  repetition (netlify-deploy: 85× the same "unpinned npx" rule) -> the leaderboard counts distinct finding types.
- Secrets/PII hits (cloudflare-deploy: 2 critical Gitleaks, 7 critical PII) all sit in `references/*.md` API docs.
  They look like documentation examples. Not verified by eye on purpose: matched lines are stripped from public reports
  (`report.public.json`), and the full reports stay private.
- Quality score range 71.5–92.2 (median 83.0). It's narrow, so differences of 1–2 points mean little. skill_type mix:
  18 guide-only, 9 script-based, 7 hybrid, 5 resource-based.

## E3 — repeat-run stability (6 skills × 3 runs, same commit)
- Quality score: identical across all runs (deterministic). Duration varies by ±5 %.
- Security findings: identical in 17/18 runs. In 1/18, SkillSpector exited with code 2 ("diagnostics redacted"), and
  that run reported 0 security findings with Security Scan = incomplete. **A transient scanner crash looks like a
  clean result unless it's detected.**
  -> The pipeline now detects `unexpected exit code` scanner errors, retries once, records attempts, and the UI shows
  "scanner error" instead of counts.

## E4 — batch 2 (43 skills: forks, NVIDIA, Composio, more categories). Catalog is now 82 skills, 8 publishers
- 43/43 completed. The rerun of systematic-debugging (crashed in E3) completed cleanly.
- Median structural score by publisher: NVIDIA 95.6 (n=6), obra 89.8, HF 87.0, ToB 86.2, vercel 83.8, openai 83.0,
  Composio 81.5, anthropics 79.2. **The rubric rewards NVIDIA's SKILL.md conventions** (version, metadata.tags,
  "Use when…" triggers, script tables). A high score means "follows the house format", not "more useful".
- Fork comparisons:
  - webapp-testing (Composio fork of anthropics; only LICENSE.txt differs): identical score 81.5 and identical
    security findings. Good, the score is reproducible across copies.
  - mcp-builder (the fork's SKILL.md and reference docs diverged): same 81.5, security high findings 4 vs 3. **The
    structural score is coarse and doesn't notice substantive content edits.**
  - skill-creator: anthropics upstream (77.2) has grown (agents/, longer SKILL.md) vs. the older copies at
    Composio/openai (81.5). Adding material can lower the structural score (efficiency/length penalties).
  - pdf: anthropics 74.0 vs openai 84.8, with different content. Within-category structural differences are real but
    narrow, and say nothing about task success.
- Conclusion: structural score is fine as a hygiene ranking but must not be presented as quality of outcomes. Tier 3
  with shared category tasks is needed for "which pdf skill is better".

## E5 — auto-generated eval cases (keyless template mode, 4 skills)
- `create-eval-dataset --full --no-llm` gave 3 cases per skill. Each prompt is the skill's own description pasted into
  a template ("Use pdf to use this skill whenever the user wants…"). There are no input files and no checkable
  outcome. `expected_script` is just the first script found (pdf -> fill_pdf_form_with_annotations.py).
- Verdict: these test whether the agent routes to and reads the skill, not whether the skill helps. Every skill gets
  its own cases, so the scores are **not comparable** across skills. LLM-generated cases (needs a key) are untested.
- -> Shared category task sets with fixtures and a deterministic grader (`shared-tasks/documents-pdf`: extract invoice
  fields, merge 2 PDFs, create 1-page PDF, plus a negative case). The grader was self-tested on correct and incorrect
  outputs, including a pypdf-merged file. `pipeline/tier3.py` replaces a skill's evals/ with the shared set and runs
  Tier 3 with the baseline arm on. It has only been dry-run, because there's no key or sandbox yet.

## E6 — batch 3 (41 skills, 9 new repos: marketing, SEO, cybersecurity, research, agents, more NVIDIA)
- 41/41 completed. Catalog: 123 skills from 17 repos. K-Dense-AI/scientific-agent-skills was skipped (460 MB > size cap).
- All Tier 1 work so far: 136 runs, 2594 s compute, median 8.1 s, p90 52 s, $0.
- Structural score over 123 skills: min 71.5, median 86.2, max 98.8.
- Two copies of planning-with-files in the same repo (.agents vs .mastracode) score 74.5 vs 76.8. Small format
  drift between copies moves the score by about 2 points, the same size as many rank gaps. Ranks within ±2 should be
  read as ties.
- render-deploy (pilot, before the retry existed) had a SkillSpector crash recorded. It was re-run with retry.
- Added `shared-tasks/testing-debugging`: a fixture module with 2 planted bugs, hidden tests, a regression-test
  task and a negative case. Self-test: buggy code scores 0.33 and 0, fixed code scores 1.0 and 1.0.

## E7 — batch 4 (24 skills, 10 new repos) and the duplicate-copy check
- 24/24 completed, 70 s compute, $0. Catalog: 148 skills from 26 repos; structural score min 71.5, median 86.2,
  max 98.8 (unchanged). Adds within-category depth: code-review now 9 skills, testing-debugging 11, frontend 16.
- Also committed: one earlier anthropics/skills `academy-guide` run from an end-to-end submit through the UI.
- Duplicate copies of one skill inside a repo, to test the noise band:
  - career-ops `.claude/` vs `.opencode/`: same tree digest, 85.5 and 85.5. Tier 1 is deterministic on identical
    content; there is no run-to-run noise to speak of.
  - career-ops `.agents/` (SKILL.md reworded, ~260 diff lines): 86.2, Δ0.7.
  - i-have-adhd `skills/` vs `.cursor/` (extra `agents/` dir only): 90.2 and 90.2, Δ0.
  - ponytail `skills/` vs `.openclaw/` (shorter description, `argument-hint` swapped for `homepage`): 86.8 vs 88.0,
    Δ1.2.
- Reading: the score moves only when content moves, and frontmatter-level drift between copies of the same skill
  moves it by 0–2.3 points (4 pairs incl. E6). The ±3 noise band stays. Its source is the score's sensitivity to
  small edits of the same skill; re-running identical content adds none.
