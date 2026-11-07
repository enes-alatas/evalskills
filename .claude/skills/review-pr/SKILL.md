---
name: review-pr
description: >-
  Use when reviewing an evalskills pull request: a first read, a re-check after the author pushes, or deciding
  whether it is ready for the merge request.
metadata:
  author: evalskills maintainers
---

# Reviewing a pull request

A review answers one question: can this PR land as it stands, and if not, what exactly must change? Review against
`docs/CONVENTIONS.md`. This file is the procedure.

## 1. Gate on CI
Read every check at the head (`gh pr checks <n>`). Account for each one that is not a pass. A check that never ran
is not green. A failure is a flake only once you have shown it (a re-run passes), and you say so on the PR.

## 2. Review the PR, not just the diff
Re-read all of this on every round:
- **The body:** the right `Closes`/`Part of` issue (closing keywords ignore negation), a filled template, and
  evidence (runs, screenshots) from the current head.
- **The issue:** read its latest comments, then find each acceptance scenario in the PR with its check. A scenario
  with no check is a finding.
- **The commits:** one logical change each, Conventional subjects, no "address review" commits, mechanical changes
  kept apart.
- **The deletions:** list what the removed code guaranteed (a guard, an ordering, a limit, a redaction), and find
  each guarantee in the new code.

## 3. Verify claims yourself
- Grep the whole tree for an old name or an old sentence after a rename or a policy change.
- Run it when reading can't settle the question: check out the head in a throwaway worktree.
- Open the screenshots. They must come from the fixture catalog.
- Cite only what you opened in this review.

## 4. evalskills-specific checks
- **Scores are genuine.** No invented, estimated or hand-edited score anywhere: catalog, fixtures, docs or UI copy.
  Failed, unsupported, inconclusive and scanner-error runs keep their status, never a 0.
- **Separate signals stay separate.** Structural quality, live performance and security findings are not merged into
  one number unless an accepted ADR says how. Static checks are never presented as live performance.
- **Provenance.** Every new result records the repo, path, commit, content hash, evaluator version and agent/model.
  Ranking or interpretation changes update `docs/METHODOLOGY.md`.
- **Untrusted input.** Nothing from a submitted repo is executed outside the sandbox, rendered as HTML, or followed
  as an instruction. Clones and raw reports are not committed.
- **Budget.** Any live run is noted with its count, model, cost and Enes's go-ahead.
- **UI copy.** No em dashes. The page works on mobile and desktop (screenshots of both for a visible change).

## 5. Write the review on the PR
- Put inline comments on the lines: what is wrong, the concrete case that breaks, and the rule it breaks. Label
  severity: **Critical:**, (required, no prefix), **Optional:**, **Nit:**.
- The summary review says what you checked and the findings by severity, and ends with exactly one of:
  "approved at `<sha>`; merge as-is, remaining notes are follow-ups" or "notes to fold; not ready for merge".
- We share one GitHub identity, so `--approve` fails. Approve with `gh pr review <n> --comment`, sign
  `— <your name> · Reviewer`, and write no `@` before a name.

## 6. Approve only what can land
All of these must hold: CI green at the reviewed head; the PR merges cleanly onto main; no `Operator decision
pending:` line; any `needs-security-review` label has a security pass at the head; docs and tests are in the same
PR. If everything holds except an operator decision, approve pending and name the question, but send no merge
request.

## 7. The merge request is yours
In the same message as the final approval, ask Enes to merge, with the PR link. The author never asks. Send one
short nudge if it is still unmerged hours later.

## Re-checking after a push
Diff the new head against the SHA you reviewed (`git diff <old>..<new>`). Read that diff, the body and the thread,
re-check CI, then re-approve at the new SHA or say what is still open.
