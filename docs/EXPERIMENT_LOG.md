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

## E9 — batch 5 (18 skills, 9 new repos: deployment and infra vendors, skill authoring)
- 18/18 completed, 38 s compute, $0, no scanner crashes. Catalog: 166 skills from 35 repos; structural score min
  71.5, median 86.2, max 98.8 (unchanged). Deployment grows from 7 to 19 skills, skill-authoring from 7 to 9.
- Vendor skills for the same job: Firebase hosting 84.2 vs app hosting 87.8; Terraform style guide 89.8 vs Pulumi
  best practices 85.0; Azure deploy 87.0 vs Azure App Service (Python) deploy 80.0; Cloudflare wrangler 86.8.
- Four skill-creator variants (anthropics 77.2, openai 81.5, ComposioHQ 81.5, microsoft 80.5) span 4.3 points.
  They share an ancestry and differ mostly in packaging, so the structural score again separates format, not
  capability; only shared live tasks could rank them on what they do.

## Change of direction — no self-authored benchmarks (2026-11-09, ADR-0006)
- Enes decided that live runs use SkillEvaluator's own content, and that shared task sets come only from public
  benchmarks we vendor and adapt. The self-authored documents-pdf and testing-debugging sets (E5, E6) are removed,
  and the proposed code-review set (#58) was closed without merging. None of them had run live.
- Live results will therefore be per-skill Skill Lift on each skill's own cases, not comparable across skills,
  until a public benchmark is vendored for a category.

## E10 — same content, two engines: the pinned Tier 1 image against d136a59 (2026-11-11, #65)
- **Why:** the catalog was scored by SkillEvaluator `d136a59`, with scanners provisioned by uv. Deployed scans now run
  in `skillevaluator-tier1@sha256:9d5483…` (#65). A score on the board should not depend on which engine ran it.
- **How:** CI's `engine-parity` job (`scripts/engine_parity.py`) took one completed catalog skill per category (11
  skills, 7 repos), checked out each at its recorded commit, and confirmed the folder digest was unchanged (11/11).
  It then scanned each through the image with the scanner sidecar's command (no network, read-only) and compared
  the summary fields. It ran 4 times on #101 (runs 43018221700, 43022639603, 43024396509, 43025349850), with the same
  outcome each time. $0, no model calls. 168–217 s of scanning per run, against 142 s for the original runs; the
  extra is mostly container start-up.
- **Scores: identical in 11/11, in every run.** Structural score, grade, the four dimensions, overall status and the
  security pill counts (`security.distinct`) were the same for every skill. The all-findings `severity_counts` match
  in 7/11; the other 4 have one more medium, which is the location finding below.
- **One finding differs, but not because of the engine.** In 4/11 skills the image run adds one medium "Skill not in
  standard location (skills/ or team-skills/)" under Schema & Repository Governance. All four catalog records come
  from the 11-07 pilot (pdf, frontend-design, requesting-code-review, test-driven-development). The pilot's 16
  scans ran on folders in place in the repository, so the check passed. Later runs scan a snapshot outside
  `skills/`, and 155 of the 180 catalog records carry the finding. Of the 25 without it, 16 are the pilot, and for
  the other 9 the reason is not yet known. The quality score doesn't count the finding (identical above). It only
  changes the "all findings" total by one. Re-scanning the pilot records would make them consistent; ranking does
  not need it.
- **One check status differs, every time:** for trailofbits `semgrep`, Code Risk Analysis is `incomplete` in the
  image, with the semgrep scanner listed as incomplete (3/3 runs that recorded it). In the catalog it is `passed`.
  Both have 0 findings. This is the one skill in the sample whose own files are semgrep rules and configs. The
  likeliest cause is the image's semgrep needing something the no-network run lacks, but that is unconfirmed. The
  leaderboard shows `incomplete` scans as such, never as a pass, so a re-scan of this skill would show "incomplete",
  not a clean result.
- -> The two engines may share the board. Scores are keyed by engine (METHODOLOGY, "Versioning and re-runs"), so a
  skill re-scanned under the image shows the image as its evaluator.

## E11 — the whole catalog re-scanned under the pinned Tier 1 image (2026-11-11, #110)
- **Why:** the `d136a59` catalog runs never ran semgrep ("Semgrep not installed; skipped", 65/180 reports), so
  Code Risk Analysis there was Bandit-only (#102). The re-scan also found that 30 skills, from the later batches,
  never ran SkillSpector ("skillspector not installed"). The board showed those as Security Scan `incomplete`, never
  as a pass. Deployed scans already use the image (#65), so the board mixed two engines with different coverage.
- **How:** CI's `catalog-rescan` job (`scripts/catalog_rescan.py`, run 43068622669) took each skill's newest record
  (166 skills, 35 repos), checked it out at its recorded commit and confirmed the folder digest was unchanged
  (166/166). It then scanned each through `skillevaluator-tier1@sha256:9d5483…` with the E10 command (no network,
  read-only). The image reports SkillEvaluator `0.7.2`, against `0.8.0` for `d136a59`. That is the image's own
  version string, not an older engine: this digest is the one upstream's 0.8.0 release bundle pins
  (`ci/bundles/0.8.0/skill-checks.yml` at `d136a59`). $0, no model calls. 4,493 s of scanning in total (median 10.7 s per skill), against 2,249 s for the
  original runs; the extra is mostly container start-up. All 166 completed. The new records are appended, and the old
  ones stay as history.
- **Structural scores: identical in 166/166.** The score, grade, dimensions and overall status are the same for every
  skill. A first run showed 4 root-level skills losing 2.5 points on discoverability. That came from the script, not
  the engine: it named the scanned folder differently from the catalog run. Fixed before this run.
- **Security changed for 51 skills, all from scanners that now run:**
  - SkillSpector (Security Scan) ran on the 30 skills that never had it: 16 `failed` with findings, 9 `passed`, and
    5 still `incomplete`. Those 5 are SkillSpector's own "partial analysis" (`reference_missing`): it couldn't
    resolve files or URLs that SKILL.md mentions. As in E1, these are mostly mentions in prose, not files the skill
    ships (see the correction below). That's a tool artifact, not the skill's fault.
  - semgrep (Code Risk Analysis) now runs. 3 skills newly `fail`, on real findings: `shell=True` in webapp-testing
    (anthropics and its ComposioHQ copy) and `child_process.exec` in obra `brainstorming`. 6 come back `incomplete`
    with semgrep as the incomplete scanner, the parse-error case from #102. One (nuwa-skill) goes `failed` →
    `passed`.
  - Code Integrity & Hygiene goes `passed` → `failed` for 2 trailofbits skills, on dead links that point outside the
    skill folder (`../../workflows/…`). The old engine didn't report them.
  - The all-findings `severity_counts` rise for 36 skills, and the security pill counts (`security.distinct`) for 25.
- **Security pills on the board change for 24 skills:** 8 go from incomplete to a clean result. 16 now show a
  medium or high finding where they showed none or a lower one.
- -> The board now has one engine with full scanner coverage, so security results are comparable across the
  catalog. Structural scores were unaffected. `incomplete` is still common: 132/166 pills. In all 132, SkillSpector
  reported partial analysis, whether the check failed or not. 118 of those cite unresolved references
  (`reference_missing`), 42 a parse limit and 2 a timeout; a skill can cite more than one. It is shown as such,
  never as a pass.
- **Correction (2026-11-12):** this entry first said the unresolved references were files outside the skill folder,
  and that scanning the whole repository would fix most of them. A check of every cited line, in the 117 skills
  whose reports list one, at their recorded commits, says otherwise. SkillSpector names up to three per check, so
  this covers 267 lines:
  - 154 name a file in backticks and 95 are other prose: `firebase.json`, "proposal.pdf",
    "2024-10-17-meeting-notes.md". These are mostly files in the user's project, or examples, not files the skill
    ships. 4 of the backtick ones are paths to sibling skills, in 3 NVIDIA doca skills (`doca-setup/SKILL.md`).
  - 9 contain a web URL or a link-like `(URL)` placeholder (7 are real URLs), which a no-network scan can't
    resolve.
  - 5 are links outside the skill folder, in 3 skills (2 trailofbits plugins, 1 openai).
  - 4 are links inside it.
  So a repository-wide scan would change at most 6 skills: the 3 with links outside the folder, and the 3 NVIDIA
  skills if SkillSpector resolves sibling paths that way, which is untested. The cause is SkillSpector's reference resolver treating mentions
  as references, the E1 finding. It's upstream behaviour, not our snapshot.

## E12 — live pilot runs 6 to 8: the agent install and the egress allowlist (2026-11-20, #121, #231, #236)
- Setup: one skill (`addyosmani__agent-skills__skills__debugging-and-error-recovery`), QuixBugs cases, OpenCode on
  NVIDIA Build through the gateway, under gVisor on the shared host. Enes ran every host step. All three runs were
  recorded `failed` (`no lift.json`), with no score. Run 6 made 0 gateway requests.
- **Run 6:** nvm's `install.sh` came from raw.githubusercontent.com. Because the trial image has git, it then
  git-cloned nvm from `github.com`, which the proxy denied. No agent was installed. `github.com` was added (#231).
- **Run 7** started 2 minutes after that merge, without the redeploy, so the proxy still had the old list. The
  redeploy step itself would not have applied it either: each service reads its config once, from a single-file
  bind mount, and `up -d` kept the old container (#234).
- **Run 8** had github.com live (checked inside the container). It ran about 24 minutes, and the proxy denied many
  more hostnames. Enes judged that allowlisting them one at a time wouldn't converge, and opened egress for the
  pilot only (#236).
- -> From here on, pilot trials have open web access, and their lift is read as an upper bound (METHODOLOGY,
  live tier). Each record's `egress` field says which mode it ran under. The proxy logs every host a trial reaches, so the hosts the install really needs can be listed. That
  list is the starting point for an allowlist outside the pilot.

